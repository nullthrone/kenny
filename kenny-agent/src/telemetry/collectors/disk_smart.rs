//! `disk_smart` section — physical disk health and reliability counters.
//!
//! Real data from `Get-PhysicalDisk` / `Get-StorageReliabilityCounter` on Windows.
//! Row shape and grading: `docs/protocol.md` § Telemetry sections (`disk_smart`).

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `disk_smart` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "disks": [] }))
    }
}

/// Portable row-shape and grading core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    use crate::protocol::Status;
    use crate::telemetry::Section;

    /// Row fields read from `Get-StorageReliabilityCounter`, paired with the
    /// property each one is read from. Every one is a lifetime value and `null`
    /// when the drive does not report it.
    pub const COUNTERS: &[(&str, &str)] = &[
        ("wear", "Wear"),
        ("temperature_c", "Temperature"),
        ("power_on_hours", "PowerOnHours"),
        ("read_errors_total", "ReadErrorsTotal"),
        ("read_errors_uncorrected", "ReadErrorsUncorrected"),
        ("write_errors_uncorrected", "WriteErrorsUncorrected"),
    ];

    /// Row fields read from `Get-PhysicalDisk` itself, paired with the
    /// PowerShell expression each one is read from.
    pub const DISK_FIELDS: &[(&str, &str)] = &[
        ("model", "[string]$_.FriendlyName"),
        ("health_status", "[string]$_.HealthStatus"),
        ("predictive_failure", "($_.HealthStatus -ne 'Healthy')"),
    ];

    /// `Get-PhysicalDisk` joined with `Get-StorageReliabilityCounter`, one row
    /// per disk, built from [`DISK_FIELDS`] and [`COUNTERS`] so the emitted
    /// keys cannot drift from the ones the fixture test checks.
    pub fn script() -> String {
        let disk: String = DISK_FIELDS
            .iter()
            .map(|(field, expr)| format!("    {field} = {expr}\n"))
            .collect();
        let counters: String = COUNTERS
            .iter()
            .map(|(field, prop)| {
                format!("    {field} = if ($rc) {{ $rc.{prop} }} else {{ $null }}\n")
            })
            .collect();
        format!(
            r#"
Get-PhysicalDisk | ForEach-Object {{
  $rc = $null
  try {{ $rc = $_ | Get-StorageReliabilityCounter -ErrorAction Stop }} catch {{}}
  [pscustomobject]@{{
{disk}{counters}  }}
}} | ConvertTo-Json -Compress
"#
        )
    }

    fn model(d: &Value) -> &str {
        d.get("model").and_then(Value::as_str).unwrap_or("disk")
    }

    /// A counter as a number; `null` (not reported) counts as zero, because it
    /// is no evidence of an error.
    fn count(d: &Value, field: &str) -> u64 {
        d.get(field).and_then(Value::as_u64).unwrap_or(0)
    }

    /// Grade the rows: a non-Healthy `health_status` is `crit`; otherwise any
    /// uncorrected read or write error is `warn`. `read_errors_total` never
    /// grades — it is mostly corrected errors, vendor-scaled, and large on
    /// healthy HDDs.
    pub fn grade(disks: &[Value]) -> (Status, String) {
        let failing: Vec<&str> = disks
            .iter()
            .filter(|d| {
                d.get("predictive_failure").and_then(Value::as_bool) == Some(true)
                    || d.get("health_status")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.eq_ignore_ascii_case("Healthy"))
            })
            .map(model)
            .collect();
        if !failing.is_empty() {
            return (
                Status::Crit,
                format!("predictive disk failure: {}", failing.join(", ")),
            );
        }

        let uncorrected: Vec<String> = disks
            .iter()
            .filter_map(|d| {
                let parts: Vec<String> = [
                    ("read", "read_errors_uncorrected"),
                    ("write", "write_errors_uncorrected"),
                ]
                .into_iter()
                .filter_map(|(label, field)| match count(d, field) {
                    0 => None,
                    n => Some(format!("{label} {n}")),
                })
                .collect();
                (!parts.is_empty()).then(|| format!("{} ({})", model(d), parts.join(", ")))
            })
            .collect();
        if !uncorrected.is_empty() {
            return (
                Status::Warn,
                format!("uncorrected errors: {}", uncorrected.join("; ")),
            );
        }

        (Status::Ok, "SMART healthy".to_string())
    }

    /// The section for already-collected rows.
    pub fn section_for(disks: Vec<Value>) -> Section {
        let (status, summary) = grade(&disks);
        Section::with_fields(status, summary, json!({ "disks": disks }))
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        /// Every row field, in emission order.
        fn fields() -> impl Iterator<Item = &'static str> {
            DISK_FIELDS.iter().chain(COUNTERS).map(|(field, _)| *field)
        }

        fn disk(health: &str, read_unc: Value, write_unc: Value, read_total: Value) -> Value {
            json!({
                "model": "D1", "health_status": health,
                "predictive_failure": health != "Healthy",
                "wear": null, "temperature_c": 30, "power_on_hours": 100,
                "read_errors_total": read_total,
                "read_errors_uncorrected": read_unc,
                "write_errors_uncorrected": write_unc,
            })
        }

        #[test]
        fn huge_read_errors_total_alone_is_ok() {
            let (status, summary) =
                grade(&[disk("Healthy", json!(0), json!(0), json!(184_223_611u64))]);
            assert_eq!(status, Status::Ok);
            assert_eq!(summary, "SMART healthy");
        }

        #[test]
        fn uncorrected_errors_warn_and_name_the_disk() {
            let (status, summary) = grade(&[disk("Healthy", json!(2), json!(1), json!(5))]);
            assert_eq!(status, Status::Warn);
            assert_eq!(summary, "uncorrected errors: D1 (read 2, write 1)");
        }

        #[test]
        fn unreported_counters_are_not_errors() {
            let (status, _) = grade(&[disk("Healthy", Value::Null, Value::Null, Value::Null)]);
            assert_eq!(status, Status::Ok);
        }

        #[test]
        fn unhealthy_disk_is_crit_over_uncorrected_errors() {
            let (status, summary) = grade(&[disk("Warning", json!(3), json!(0), json!(3))]);
            assert_eq!(status, Status::Crit);
            assert_eq!(summary, "predictive disk failure: D1");
        }

        #[test]
        fn script_emits_every_row_field() {
            let script = script();
            for field in fields() {
                assert!(
                    script.contains(&format!("    {field} = ")),
                    "script lacks {field}"
                );
            }
            assert!(!script.contains("reallocated_sectors"));
        }

        /// The contract's example rows are exactly the fields the collector
        /// emits, and grade to the fixture's own status and summary — a renamed,
        /// added or dropped field fails here, not on the dashboard.
        #[test]
        fn the_fixture_is_what_the_collector_produces() {
            let fixture: Value = serde_json::from_str(
                &std::fs::read_to_string(
                    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../docs/fixtures/telemetry_snapshot.json"),
                )
                .expect("read the fixture"),
            )
            .expect("parse the fixture");
            let expected = &fixture["snapshot"]["disk_smart"];
            let rows = expected["disks"].as_array().expect("disks").clone();
            assert!(!rows.is_empty());

            let mut emitted: Vec<&str> = fields().collect();
            emitted.sort_unstable();
            for row in &rows {
                let mut keys: Vec<&str> = row
                    .as_object()
                    .expect("row")
                    .keys()
                    .map(String::as_str)
                    .collect();
                keys.sort_unstable();
                assert_eq!(keys, emitted);
            }

            assert_eq!(&section_for(rows).into_value(), expected);
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    pub fn collect() -> Section {
        let Some(v) = winps::run_json(&core::script()) else {
            return Section::with_fields(Status::Ok, "SMART unavailable", json!({ "disks": [] }));
        };
        core::section_for(winps::as_array(v))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_smart_section_is_valid() {
        assert!(collect().into_value()["disks"].is_array());
    }
}
