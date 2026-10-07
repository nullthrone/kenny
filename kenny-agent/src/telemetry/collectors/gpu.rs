//! `gpu` section — graphics adapter inventory and raw health facts.
//!
//! STUB: returns the contract shape with an empty list. The real collector (WMI +
//! `nvidia-smi` on Windows, sysfs + `nvidia-smi` on Linux) replaces the body of
//! [`collect`]. The agent reports facts and never grades: `status` is always `ok`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `gpu` section.
// STUB BODY: replace with the real collection.
pub fn collect() -> Section {
    Section::with_fields(
        Status::Ok,
        "0 GPU(s)",
        json!({
            "gpus": [],
            "truncated": false,
            "errors": [],
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gpu_section_is_valid() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["gpus"].is_array());
        assert!(v["errors"].is_array());
    }
}
