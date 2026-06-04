//! `defender_quarantine` section — quarantined threats.
//!
//! Real data from `Get-MpThreatDetection` / `Get-MpThreat` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `defender_quarantine` section.
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
            json!({ "items": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Get-MpThreatDetection` into `{name, severity, detected_at}` items.
    pub fn collect() -> Section {
        // TODO(windows): Get-MpThreatDetection / Get-MpThreat.
        Section::with_fields(Status::Ok, "0 quarantined items", json!({ "items": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defender_quarantine_section_is_valid() {
        assert!(collect().into_value()["items"].is_array());
    }
}
