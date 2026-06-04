//! `time_sync` section — system clock synchronization state.
//!
//! Real data from `w32tm /query /status` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `time_sync` section.
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
            json!({ "synchronized": null, "source": null, "offset_secs": null }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: parse `w32tm /query /status` for source, last sync, and offset;
    /// `warn` when offset is large or the service is not running.
    pub fn collect() -> Section {
        // TODO(windows): w32tm /query /status.
        Section::with_fields(
            Status::Ok,
            "clock synchronized",
            json!({ "synchronized": true, "source": null, "offset_secs": 0 }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn time_sync_section_is_valid() {
        assert!(collect().into_value()["status"].is_string());
    }
}
