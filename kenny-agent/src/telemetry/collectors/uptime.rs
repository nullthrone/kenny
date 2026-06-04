//! `uptime` section — system uptime / boot time. Portable via `sysinfo`.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Uptime in seconds beyond which we surface a `warn` (nudge a reboot).
const WARN_AFTER_SECS: u64 = 60 * 60 * 24 * 30;

/// Collect the `uptime` section.
pub fn collect() -> Section {
    let uptime_secs = System::uptime();
    let boot = System::boot_time();
    let days = uptime_secs / 86_400;
    let hours = (uptime_secs % 86_400) / 3_600;
    let status = if uptime_secs >= WARN_AFTER_SECS {
        Status::Warn
    } else {
        Status::Ok
    };
    Section::with_fields(
        status,
        format!("up {days}d {hours}h"),
        json!({
            "uptime_secs": uptime_secs,
            "boot_time_unix": boot,
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uptime_section_is_valid() {
        let v = collect().into_value();
        assert!(v["uptime_secs"].as_u64().is_some());
    }
}
