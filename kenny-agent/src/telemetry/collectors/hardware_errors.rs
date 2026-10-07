//! `hardware_errors` section — hardware-relevant events in a rolling 14-day window.
//!
//! STUB: returns the contract shape with empty lists. The real collector (Windows event
//! log, Linux journal + EDAC + PCIe AER) replaces the body of [`collect`]. The agent
//! reports facts and never grades: `status` is always `ok` (ADR-0058).

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Rolling window the agent queries, in days.
const WINDOW_DAYS: u32 = 14;

/// Collect the `hardware_errors` section.
// STUB BODY: replace with the real collection.
pub fn collect() -> Section {
    Section::with_fields(
        Status::Ok,
        format!("0 hardware-relevant events in {WINDOW_DAYS}d"),
        json!({
            "window_days": WINDOW_DAYS,
            "effective_window_days": null,
            "oldest_event_utc": null,
            "sources": [],
            "groups": [],
            "truncated": false,
            "truncated_count": 0,
            "edac": [],
            "aer": [],
            "errors": [],
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hardware_errors_section_is_valid() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["window_days"], 14);
        for list in ["sources", "groups", "edac", "aer", "errors"] {
            assert!(v[list].is_array(), "{list} must be a list");
        }
    }
}
