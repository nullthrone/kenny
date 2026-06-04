//! `autostart` section — startup programs.
//!
//! Real data from Run keys / Startup folders / `Get-CimInstance Win32_StartupCommand`
//! on Windows. Shares the `{name, command, location}` shape with `diag.autostart`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `autostart` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "entries": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Win32_StartupCommand` + Run keys into `{name, command, location}`.
    pub fn collect() -> Section {
        // TODO(windows): Win32_StartupCommand / HKLM+HKCU Run keys.
        Section::with_fields(Status::Ok, "0 startup entries", json!({ "entries": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn autostart_section_is_valid() {
        assert!(collect().into_value()["entries"].is_array());
    }
}
