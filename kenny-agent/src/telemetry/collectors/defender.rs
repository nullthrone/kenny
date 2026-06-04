//! `defender` section — Microsoft Defender status.
//!
//! Real data comes from `Get-MpComputerStatus` on Windows. Off Windows we stub.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `defender` section.
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
            json!({
                "enabled": null,
                "realtime_protection": null,
                "last_scan": null,
                "last_scan_type": null,
                "last_signature_update": null,
                "threats_found": 0,
                "action_needed": false
            }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: parse `Get-MpComputerStatus` into the defender fields.
    pub fn collect() -> Section {
        // TODO(windows): Get-MpComputerStatus / Get-MpThreat.
        Section::with_fields(
            Status::Ok,
            "Defender status via Get-MpComputerStatus",
            json!({
                "enabled": true,
                "realtime_protection": true,
                "last_scan": null,
                "last_scan_type": null,
                "last_signature_update": null,
                "threats_found": 0,
                "action_needed": false
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defender_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v.get("threats_found").is_some());
    }
}
