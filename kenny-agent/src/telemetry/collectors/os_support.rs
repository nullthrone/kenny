//! `os_support` section — OS edition/build and end-of-support posture.
//!
//! Portable basics (name/version) via `sysinfo`; Windows enriches with build and
//! support lifecycle.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `os_support` section.
pub fn collect() -> Section {
    let name = System::name().unwrap_or_else(|| "unknown".to_string());
    let version = System::os_version().unwrap_or_else(|| "unknown".to_string());
    let long = System::long_os_version().unwrap_or_else(|| name.clone());

    #[cfg(windows)]
    {
        // TODO(windows): map build number to support end date; set warn/crit near EOL.
        Section::with_fields(
            Status::Ok,
            long,
            json!({ "name": name, "version": version, "build": null, "eol": null }),
        )
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            long,
            json!({ "name": name, "version": version, "build": null, "eol": null }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_support_section_is_valid() {
        let v = collect().into_value();
        assert!(v["name"].is_string());
    }
}
