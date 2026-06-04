//! `thermals` section — component temperatures. Portable via `sysinfo` where the
//! platform exposes sensors (Linux); Windows would use LibreHardwareMonitor/WMI.

use serde_json::json;
use sysinfo::Components;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Warn/crit thresholds in Celsius.
const WARN_C: f32 = 85.0;
const CRIT_C: f32 = 95.0;

/// Collect the `thermals` section.
pub fn collect() -> Section {
    let components = Components::new_with_refreshed_list();
    let mut sensors = Vec::new();
    let mut hottest: f32 = 0.0;
    for c in components.list() {
        let temp = c.temperature();
        if temp.is_finite() {
            hottest = hottest.max(temp);
            sensors.push(json!({ "label": c.label(), "temperature_c": temp }));
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thermals_section_is_valid() {
        let v = collect().into_value();
        assert!(v["sensors"].is_array());
    }
}
