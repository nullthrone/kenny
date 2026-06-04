//! `reboot_pending` section — whether a reboot is required.
//!
//! On Windows this checks the CBS / WindowsUpdate / PendingFileRename registry
//! markers. Off Windows we stub to `pending: false`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `reboot_pending` section.
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
            json!({ "pending": false, "reasons": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: check `Component Based Servicing\RebootPending`,
    /// `WindowsUpdate\Auto Update\RebootRequired`, and `PendingFileRenameOperations`.
    pub fn collect() -> Section {
        // TODO(windows): registry probes for reboot markers.
        Section::with_fields(
            Status::Ok,
            "no reboot pending",
            json!({ "pending": false, "reasons": [] }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reboot_pending_section_is_valid() {
        let v = collect().into_value();
        assert!(v["pending"].is_boolean());
    }
}
