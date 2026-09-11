//! `disk` section — per-volume capacity. Portable via `sysinfo`.

use serde_json::{json, Value};
use sysinfo::Disks;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Percent of `total` in use, given `total`/`free` bytes as `sysinfo` reports them.
///
/// `free` is not guaranteed to be `<= total`: quota-managed, thin-provisioned, or
/// network/overlay filesystems can report a larger available space than total (and a
/// resize race can do the same even for a plain local disk), so this saturates
/// instead of underflowing the `u64` subtraction.
fn percent_used(total: u64, free: u64) -> u64 {
    if total > 0 {
        ((total.saturating_sub(free) as f64 / total as f64) * 100.0).round() as u64
    } else {
        0
    }
}

/// Per-volume `{mount, total_bytes, free_bytes, percent_used}` list.
///
/// Shared with the `fs_disk_usage` handler.
pub fn volumes() -> Vec<Value> {
    let disks = Disks::new_with_refreshed_list();
    disks
        .list()
        .iter()
        .map(|d| {
            let total = d.total_space();
            let free = d.available_space();
            json!({
                "mount": d.mount_point().to_string_lossy(),
                "total_bytes": total,
                "free_bytes": free,
                "percent_used": percent_used(total, free),
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
    let summary = match vols
        .iter()
        .max_by_key(|v| v["percent_used"].as_u64().unwrap_or(0))
    {
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

    #[test]
    fn percent_used_reports_zero_for_an_empty_volume() {
        assert_eq!(percent_used(0, 0), 0);
    }

    #[test]
    fn percent_used_rounds_the_normal_case() {
        assert_eq!(percent_used(200, 50), 75);
    }

    #[test]
    fn percent_used_saturates_instead_of_underflowing_when_free_exceeds_total() {
        // Quota-managed, thin-provisioned, or network/overlay filesystems (and a
        // resize race on a plain disk) can report available_space() > total_space().
        assert_eq!(percent_used(100, 200), 0);
    }
}
