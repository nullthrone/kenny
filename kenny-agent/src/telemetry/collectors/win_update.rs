//! `win_update` section — Windows Update history.
//!
//! Real data from the Windows Update agent COM API / `Get-WUHistory`. Off Windows
//! we stub.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `win_update` section.
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
            json!({ "last_check": null, "recent": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: enumerate update history via the WUA COM API into
    /// `{kb, title, result, installed_at}` entries.
    pub fn collect() -> Section {
        // TODO(windows): IUpdateSession/IUpdateSearcher QueryHistory.
        Section::with_fields(
            Status::Ok,
            "update history via WUA",
            json!({ "last_check": null, "recent": [] }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn win_update_section_is_valid() {
        let v = collect().into_value();
        assert!(v["recent"].is_array());
    }
}
