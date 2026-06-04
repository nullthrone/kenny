//! `disk` section — per-volume capacity. Portable via `sysinfo`.

use serde_json::{json, Value};
use sysinfo::Disks;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Per-volume `{mount, total_bytes, free_bytes, percent_used}` list.
///
/// Shared with the `fs.disk_usage` handler.
pub fn volumes() -> Vec<Value> {
    let disks = Disks::new_with_refreshed_list();
    disks
        .list()
        .iter()
        .map(|d| {
            let total = d.total_space();
            let free = d.available_space();
            let percent_used = if total > 0 {
                (((total - free) as f64 / total as f64) * 100.0).round() as u64
            } else {
                0
            };
            json!({
                "mount": d.mount_point().to_string_lossy(),
                "total_bytes": total,
                "free_bytes": free,
                "percent_used": percent_used,
            })
        })
        .collect()
}

/// Collect the `disk` section.
pub fn collect() -> Section {
    let vols = volumes();
    let worst = vols
        .iter()
        .filter_map(|v| v["percent_used"].as_u64())
        .max()
        .unwrap_or(0);
    let status = if worst >= 90 {
        Status::Crit
    } else if worst >= 80 {
        Status::Warn
    } else {
        Status::Ok
    };
    let summary = match vols.iter().max_by_key(|v| v["percent_used"].as_u64().unwrap_or(0)) {
        Some(v) => format!(
            "{} {}% full",
            v["mount"].as_str().unwrap_or("?"),
            v["percent_used"].as_u64().unwrap_or(0)
        ),
        None => "no volumes detected".to_string(),
    };
    Section::with_fields(status, summary, json!({ "volumes": vols, "top_dirs": [] }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_section_is_valid() {
        let s = collect();
        let v = s.into_value();
        assert!(v["status"].is_string());
        assert!(v["volumes"].is_array());
    }
}
