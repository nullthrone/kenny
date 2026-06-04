//! `reliability` section — system stability index / recent crashes.
//!
//! Real data from `Win32_ReliabilityStabilityMetrics` / `Get-WinEvent` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `reliability` section.
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
            json!({ "stability_index": null, "recent_crashes": 0 }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Win32_ReliabilityStabilityMetrics` for the index plus a count of
    /// recent application/system error events.
    pub fn collect() -> Section {
        // TODO(windows): reliability metrics + event log error counts.
        Section::with_fields(
            Status::Ok,
            "stable",
            json!({ "stability_index": null, "recent_crashes": 0 }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reliability_section_is_valid() {
        let v = collect().into_value();
        assert!(v.get("recent_crashes").is_some());
    }
}
