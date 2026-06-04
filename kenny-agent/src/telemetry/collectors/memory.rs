//! `memory` section — RAM/swap usage. Portable via `sysinfo`.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `memory` section.
pub fn collect() -> Section {
    let mut sys = System::new();
    sys.refresh_memory();
    let total = sys.total_memory();
    let used = sys.used_memory();
    let percent_used = if total > 0 {
        ((used as f64 / total as f64) * 100.0).round() as u64
    } else {
        0
    };
    let status = if percent_used >= 95 {
        Status::Crit
    } else if percent_used >= 85 {
        Status::Warn
    } else {
        Status::Ok
    };
    Section::with_fields(
        status,
        format!("{percent_used}% RAM used"),
        json!({
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": sys.available_memory(),
            "percent_used": percent_used,
            "swap_total_bytes": sys.total_swap(),
            "swap_used_bytes": sys.used_swap(),
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_section_is_valid() {
        let v = collect().into_value();
        assert!(["ok", "warn", "crit"].contains(&v["status"].as_str().unwrap()));
        assert!(v["total_bytes"].as_u64().is_some());
    }
}
