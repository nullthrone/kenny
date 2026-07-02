//! `thermals` section — component temperatures.
//!
//! On Windows a single sensor source is never enough: ACPI thermal zones
//! (`MSAcpi_ThermalZoneTemperature`) are populated on most laptops/OEM prebuilts but
//! almost never on desktop mainboards, where the real sensors live behind the AMD/Intel
//! SMU, the Super-I/O chip, NVML and SMART. Reading only ACPI zones therefore reports
//! `no temperature sensors` on a typical desktop even though many sensors exist.
//!
//! Instead we merge several driverless, best-effort sources — each already reachable
//! without a kernel driver or admin — and let the plausibility filter drop the empties:
//!   * ACPI thermal zones (laptops/OEM),
//!   * the NVIDIA GPU via `nvidia-smi` (ships with the GPU driver),
//!   * storage/SSD temperatures via `Get-StorageReliabilityCounter`,
//!   * LibreHardwareMonitor / OpenHardwareMonitor's WMI namespace when a power user runs
//!     it — that one source yields full CPU/board/VRM/RAM/GPU parity for free.
//!
//! Any source that is absent simply contributes nothing. Off Windows it is portable via
//! `sysinfo`, which exposes sensors where the platform does (Linux hwmon).

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

/// Parse `nvidia-smi --query-gpu=name,temperature.gpu --format=csv,noheader,nounits`
/// output into `(label, temperature_c)` rows. Each non-empty line is `<name>, <temp>`;
/// the temperature is the last comma-separated field and the name is everything before it
/// (GPU model names contain no commas). Lines that do not parse are skipped.
#[cfg(any(windows, test))]
fn parse_nvidia_smi(out: &str) -> Vec<(String, f32)> {
    out.lines()
        .filter_map(|line| {
            let line = line.trim();
            let idx = line.rfind(',')?;
            let temp: f32 = line[idx + 1..].trim().parse().ok()?;
            let name = line[..idx].trim();
            let label = if name.is_empty() {
                "GPU".to_string()
            } else {
                format!("GPU: {name}")
            };
            Some((label, temp))
        })
        .collect()
}

/// Map a `ConvertTo-Json` value whose rows are `{ label, temperature_c }` into
/// `(label, temperature_c)` rows. `ConvertTo-Json` collapses a one-element collection to a
/// bare object, so both an array and a single object are accepted. Rows without a numeric
/// `temperature_c` are dropped; a missing/empty `label` falls back to `default_label`.
#[cfg(any(windows, test))]
fn rows_from_json(value: serde_json::Value, default_label: &str) -> Vec<(String, f32)> {
    use serde_json::Value;
    let items = match value {
        Value::Array(items) => items,
        Value::Null => Vec::new(),
        other => vec![other],
    };
    items
        .into_iter()
        .filter_map(|row| {
            let temp = row.get("temperature_c").and_then(Value::as_f64)? as f32;
            let label = row
                .get("label")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or(default_label)
                .to_string();
            Some((label, temp))
        })
        .collect()
}

/// Drop rows whose label (trimmed, case-insensitive) was already seen, keeping the first.
/// Sources overlap — e.g. LibreHardwareMonitor and `nvidia-smi` both report the GPU — and
/// the first source in merge order wins.
#[cfg(any(windows, test))]
fn dedup_by_label(rows: Vec<(String, f32)>) -> Vec<(String, f32)> {
    let mut seen = std::collections::HashSet::new();
    rows.into_iter()
        .filter(|(label, _)| seen.insert(label.trim().to_ascii_lowercase()))
        .collect()
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
            .filter_map(|c| Some((c.label().to_string(), c.temperature()?)));
        section_from_sensors(rows)
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Merge every driverless temperature source. Each is best-effort — a failed, blocked
    /// or absent source (no NVIDIA GPU, no LHM running, no ACPI zones) returns an empty
    /// list rather than an error — and all share `winps`'s per-probe timeout, so one
    /// wedged query can never stall the snapshot. LHM/OHM is queried first so its rich
    /// labels win the label dedup over the coarser per-device sources.
    pub fn collect() -> Section {
        let mut rows: Vec<(String, f32)> = Vec::new();
        rows.extend(lhm_ohm());
        rows.extend(acpi_zones());
        rows.extend(nvidia_gpu());
        rows.extend(storage_temps());
        section_from_sensors(dedup_by_label(rows))
    }

    /// ACPI thermal zones via `MSAcpi_ThermalZoneTemperature` in the `root/WMI` namespace.
    /// `CurrentTemperature` is reported in tenths of a Kelvin, so Celsius is
    /// `value / 10 - 273.15`. Populated on most laptops/OEM prebuilts, rarely on desktops.
    fn acpi_zones() -> Vec<(String, f32)> {
        let script = r#"
Get-CimInstance -Namespace 'root/WMI' -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |
  ForEach-Object {
    [pscustomobject]@{
      label         = [string]$_.InstanceName
      temperature_c = [math]::Round(($_.CurrentTemperature / 10.0) - 273.15, 1)
    }
  } | ConvertTo-Json -Compress
"#;
        winps::run_json(script)
            .map(|v| rows_from_json(v, "thermal zone"))
            .unwrap_or_default()
    }

    /// NVIDIA GPU temperature via `nvidia-smi`, which ships with the GPU driver and needs
    /// no admin. Absent on machines without an NVIDIA GPU (the binary is not on PATH), in
    /// which case `run_command` returns `None` and this contributes nothing.
    fn nvidia_gpu() -> Vec<(String, f32)> {
        winps::run_command(
            "nvidia-smi",
            &[
                "--query-gpu=name,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
        )
        .map(|out| parse_nvidia_smi(&out))
        .unwrap_or_default()
    }

    /// Storage/SSD temperatures via `Get-StorageReliabilityCounter`. Drives that do not
    /// report a temperature surface `null`, which `rows_from_json` drops.
    fn storage_temps() -> Vec<(String, f32)> {
        let script = r#"
Get-PhysicalDisk | ForEach-Object {
    $c = $_ | Get-StorageReliabilityCounter
    [pscustomobject]@{
      label         = 'SSD: ' + $_.FriendlyName
      temperature_c = $c.Temperature
    }
  } | ConvertTo-Json -Compress
"#;
        winps::run_json(script)
            .map(|v| rows_from_json(v, "storage"))
            .unwrap_or_default()
    }

    /// Every temperature sensor exposed by LibreHardwareMonitor or OpenHardwareMonitor,
    /// if one is running. Their WMI providers cover CPU/board/VRM/RAM/GPU — full parity —
    /// but only exist while the tool runs, so this is purely opportunistic.
    fn lhm_ohm() -> Vec<(String, f32)> {
        ["root/LibreHardwareMonitor", "root/OpenHardwareMonitor"]
            .iter()
            .flat_map(|ns| {
                let script = format!(
                    r#"
Get-CimInstance -Namespace '{ns}' -ClassName Sensor -ErrorAction Stop |
  Where-Object {{ $_.SensorType -eq 'Temperature' }} |
  ForEach-Object {{
    [pscustomobject]@{{
      label         = [string]$_.Name
      temperature_c = $_.Value
    }}
  }} | ConvertTo-Json -Compress
"#
                );
                winps::run_json(&script)
                    .map(|v| rows_from_json(v, "sensor"))
                    .unwrap_or_default()
            })
            .collect()
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
    fn parse_nvidia_smi_reads_name_and_temp() {
        let rows = parse_nvidia_smi("NVIDIA GeForce RTX 5080, 53\n");
        assert_eq!(
            rows,
            vec![("GPU: NVIDIA GeForce RTX 5080".to_string(), 53.0)]
        );

        // Multiple GPUs, a blank trailing line, and an unparsable line are handled.
        let rows = parse_nvidia_smi("GPU A, 40\nGPU B, 61\n\nnot-a-row\n");
        assert_eq!(
            rows,
            vec![
                ("GPU: GPU A".to_string(), 40.0),
                ("GPU: GPU B".to_string(), 61.0),
            ]
        );
    }

    #[test]
    fn rows_from_json_accepts_object_and_array() {
        // ConvertTo-Json collapses a single row to a bare object.
        let one = rows_from_json(json!({ "label": "CPU", "temperature_c": 61.0 }), "sensor");
        assert_eq!(one, vec![("CPU".to_string(), 61.0)]);

        // Array of rows; a null temperature (e.g. a drive without a sensor) is dropped and
        // an empty label falls back to the default.
        let many = rows_from_json(
            json!([
                { "label": "SSD: Lexar", "temperature_c": 53.0 },
                { "label": "", "temperature_c": 44.0 },
                { "label": "no temp", "temperature_c": null },
            ]),
            "storage",
        );
        assert_eq!(
            many,
            vec![
                ("SSD: Lexar".to_string(), 53.0),
                ("storage".to_string(), 44.0),
            ]
        );
    }

    #[test]
    fn dedup_by_label_keeps_first_case_insensitively() {
        let rows = dedup_by_label(vec![
            ("GPU Core".to_string(), 55.0),
            ("gpu core".to_string(), 999.0), // same sensor from another source, dropped
            ("CPU".to_string(), 61.0),
        ]);
        assert_eq!(
            rows,
            vec![("GPU Core".to_string(), 55.0), ("CPU".to_string(), 61.0)]
        );
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
