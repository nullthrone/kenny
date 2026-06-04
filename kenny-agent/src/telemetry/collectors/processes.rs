//! `processes` section — top processes by memory. Portable via `sysinfo`.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Number of top processes (by memory) to report.
const TOP_N: usize = 15;

/// Collect the `processes` section.
pub fn collect() -> Section {
    let mut sys = System::new();
    sys.refresh_processes();
    let mut procs: Vec<_> = sys.processes().values().collect();
    procs.sort_by_key(|p| std::cmp::Reverse(p.memory()));
    let count = procs.len();

    let top: Vec<_> = procs
        .into_iter()
        .take(TOP_N)
        .map(|p| {
            json!({
                "pid": p.pid().as_u32(),
                "name": p.name().to_string_lossy(),
                "cpu": p.cpu_usage(),
                "mem_bytes": p.memory(),
            })
        })
        .collect();

    Section::with_fields(
        Status::Ok,
        format!("{count} processes"),
        json!({ "count": count, "processes": top }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn processes_section_has_entries() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["processes"].is_array());
    }
}
