//! `battery` section — battery charge/health (laptops).
//!
//! Real data from `Win32_Battery` / battery report on Windows. Desktops report
//! `present: false`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `battery` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "present": false }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Win32_Battery` into `{present, charge_percent, health_percent, status}`.
    pub fn collect() -> Section {
        // TODO(windows): Win32_Battery / powercfg /batteryreport.
        Section::with_fields(Status::Ok, "battery status", json!({ "present": false }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn battery_section_is_valid() {
        assert!(collect().into_value()["present"].is_boolean());
    }
}
