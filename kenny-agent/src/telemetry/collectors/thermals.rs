//! `thermals` section — component temperatures.
//!
//! On Windows this reads `MSAcpi_ThermalZoneTemperature` (ACPI thermal zones) via WMI.
//! Coverage is not universal — some laptops/OEMs do not populate ACPI thermal zones,
//! in which case the section reports `no temperature sensors`. Off Windows it is
//! portable via `sysinfo`, which exposes sensors where the platform does (Linux hwmon).

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Warn/crit thresholds in Celsius.
const WARN_C: f32 = 85.0;
const CRIT_C: f32 = 95.0;

/// Plausible band for a real temperature reading in Celsius. Firmware quirks sometimes
/// surface constant 0 °C or wildly out-of-range values; dropping them avoids both false
/// sensors and false CRITs.
const MIN_PLAUSIBLE_C: f32 = 0.0;
const MAX_PLAUSIBLE_C: f32 = 150.0;

/// Collect the `thermals` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        portable::collect()
    }
}

/// Build the section from `(label, temperature_c)` rows, applying the plausibility
/// filter and the shared warn/crit thresholds. Shared by both platform paths.
fn section_from_sensors(rows: impl IntoIterator<Item = (String, f32)>) -> Section {
    let mut sensors = Vec::new();
    let mut hottest: f32 = 0.0;
    for (label, temp) in rows {
        if temp.is_finite() && temp > MIN_PLAUSIBLE_C && temp < MAX_PLAUSIBLE_C {
            hottest = hottest.max(temp);
            sensors.push(json!({ "label": label, "temperature_c": temp }));
        }
    }

    if sensors.is_empty() {
        return Section::with_fields(
            Status::Ok,
            "no temperature sensors",
            json!({ "sensors": [] }),
        );
    }

    let status = if hottest >= CRIT_C {
        Status::Crit
    } else if hottest >= WARN_C {
        Status::Warn
    } else {
        Status::Ok
    };
    Section::with_fields(
        status,
        format!("hottest {hottest:.0}C"),
        json!({ "sensors": sensors }),
    )
}

#[cfg(not(windows))]
mod portable {
    use super::*;
    use sysinfo::Components;

    /// Collect temperatures via `sysinfo` (Linux exposes them through hwmon).
    pub fn collect() -> Section {
        let components = Components::new_with_refreshed_list();
        let rows = components
            .list()
            .iter()
            .map(|c| (c.label().to_string(), c.temperature()));
        section_from_sensors(rows)
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    /// Read ACPI thermal zones via `MSAcpi_ThermalZoneTemperature` in the `root/WMI`
    /// namespace. `CurrentTemperature` is reported in tenths of a Kelvin, so Celsius is
    /// `value / 10 - 273.15`. On query failure (no thermal zone, blocked, timeout) the
    /// caller falls back to the contract-safe "no temperature sensors" default.
    pub fn collect() -> Section {
        let script = r#"
Get-CimInstance -Namespace 'root/WMI' -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |
  ForEach-Object {
    [pscustomobject]@{
      label         = [string]$_.InstanceName
      temperature_c = [math]::Round(($_.CurrentTemperature / 10.0) - 273.15, 1)
    }
  } | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "no temperature sensors",
                json!({ "sensors": [] }),
            );
        };

        let rows = winps::as_array(v).into_iter().filter_map(|row| {
            let temp = row.get("temperature_c").and_then(Value::as_f64)? as f32;
            let label = row
                .get("label")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("thermal zone")
                .to_string();
            Some((label, temp))
        });
        section_from_sensors(rows)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thermals_section_is_valid() {
        let v = collect().into_value();
        assert!(v["sensors"].is_array());
    }

    #[test]
    fn ok_below_warn_threshold() {
        let v = section_from_sensors([("CPU".to_string(), 61.0)]).into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "hottest 61C");
        assert_eq!(v["sensors"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn warn_then_crit_on_hottest_sensor() {
        let warn =
            section_from_sensors([("a".to_string(), 40.0), ("b".to_string(), 88.0)]).into_value();
        assert_eq!(warn["status"], "warn");

        let crit =
            section_from_sensors([("a".to_string(), 40.0), ("b".to_string(), 97.0)]).into_value();
        assert_eq!(crit["status"], "crit");
    }

    #[test]
    fn implausible_readings_are_dropped() {
        // Constant 0 °C (firmware quirk), NaN, and out-of-band values are filtered out,
        // leaving no sensors rather than a bogus reading.
        let v = section_from_sensors([
            ("zero".to_string(), 0.0),
            ("nan".to_string(), f32::NAN),
            ("absurd".to_string(), 999.0),
        ])
        .into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "no temperature sensors");
        assert!(v["sensors"].as_array().unwrap().is_empty());
    }
}
