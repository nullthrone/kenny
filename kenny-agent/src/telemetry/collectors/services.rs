//! `services` section — Windows service inventory.
//!
//! Real data comes from `Get-Service`/CIM on Windows. Off Windows we stub to
//! `n/a`; the section shape (`services: [{name, display, status, start}]`) matches
//! the `diag.services` tool result.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `services` section.
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
            json!({ "services": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: enumerate services via `Get-CimInstance Win32_Service` into
    /// `{name, display, status, start}`.
    pub fn collect() -> Section {
        // TODO(windows): query Win32_Service; flag auto-start services that are stopped.
        Section::with_fields(Status::Ok, "0 services", json!({ "services": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn services_section_is_valid() {
        let v = collect().into_value();
        assert!(v["services"].is_array());
    }
}
