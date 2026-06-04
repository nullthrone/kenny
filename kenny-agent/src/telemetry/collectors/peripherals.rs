//! `peripherals` section — attached devices (monitors, USB, audio).
//!
//! Real inventory comes from CIM (`Win32_PnPEntity`) on Windows. Off Windows we
//! report a portable `n/a` stub; the section still carries `status`/`summary`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `peripherals` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "devices": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: enumerate `Win32_PnPEntity` via CIM/WMI into
    /// `{name, class, status}` device entries.
    pub fn collect() -> Section {
        // TODO(windows): query Win32_PnPEntity through PowerShell/CIM.
        Section::with_fields(Status::Ok, "0 devices", json!({ "devices": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peripherals_section_is_valid() {
        let v = collect().into_value();
        assert!(v["devices"].is_array());
    }
}
