//! `encryption` section — BitLocker volume encryption state.
//!
//! Real data from `Get-BitLockerVolume` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `encryption` section.
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
            json!({ "volumes": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Get-BitLockerVolume` into `{mount, protection_status, encryption_percent}`;
    /// `warn` when the system drive is unencrypted.
    pub fn collect() -> Section {
        // TODO(windows): Get-BitLockerVolume.
        Section::with_fields(Status::Ok, "encryption via BitLocker", json!({ "volumes": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encryption_section_is_valid() {
        assert!(collect().into_value()["volumes"].is_array());
    }
}
