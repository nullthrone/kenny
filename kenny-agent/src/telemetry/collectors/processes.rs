//! `processes` section — top processes by memory. Portable via `sysinfo`.

use serde_json::json;
use sysinfo::{ProcessesToUpdate, System};

use crate::protocol::Status;
use crate::telemetry::Section;

/// Number of top processes (by memory) to report.
const TOP_N: usize = 15;

/// Collect the `processes` section.
///
/// While a protected game is running (anti-cheat coexistence, ADR-0039) this reports a
/// "paused" section with no process list instead of enumerating the whole machine — the
/// enumeration is one of the behaviours a kernel anti-cheat flags.
pub fn collect() -> Section {
    collect_inner(crate::coexist::game_active())
}

fn collect_inner(paused: bool) -> Section {
    if paused {
        return Section::with_fields(
            Status::Ok,
            crate::coexist::paused_summary(),
            json!({ "count": 0, "processes": [], "paused": true }),
        );
    }

    let mut sys = System::new();
    sys.refresh_processes(ProcessesToUpdate::All, true);
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
    fn active_section_has_entries() {
        let v = collect_inner(false).into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["processes"].is_array());
    }

    #[test]
    fn paused_section_lists_no_processes() {
        let v = collect_inner(true).into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["paused"], true);
        assert_eq!(v["processes"].as_array().unwrap().len(), 0);
        assert!(v["summary"].as_str().unwrap().contains("paused"));
    }
}
