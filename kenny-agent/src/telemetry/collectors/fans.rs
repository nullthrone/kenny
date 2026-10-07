//! `fans` section — measured fan speeds, sampled in a short burst.
//!
//! STUB: returns the contract shape with an empty list. The real collector (hwmon on
//! Linux, LibreHardwareMonitor's WMI namespace on Windows) replaces the body of
//! [`collect`]. The agent reports facts and never grades: `status` is always `ok`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Gap between the samples of one burst, in milliseconds.
const SAMPLE_INTERVAL_MS: u64 = 1000;

/// Collect the `fans` section.
// STUB BODY: replace with the real collection.
pub fn collect() -> Section {
    Section::with_fields(
        Status::Ok,
        "0 fans read",
        json!({
            "sources_tried": [],
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "fans": [],
            "truncated": false,
            "errors": [],
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fans_section_is_valid() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["sample_interval_ms"], 1000);
        assert!(v["fans"].is_array());
        assert!(v["sources_tried"].is_array());
    }
}
