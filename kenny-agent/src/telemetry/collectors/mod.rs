//! Telemetry collectors — one module per section (see `../docs/protocol.md`).
//!
//! Mandatory sections (`disk`, `peripherals`, `network`, `routing`, `processes`,
//! `services`, `defender`, `win_update`) plus hardware/security/update/operations
//! sections. Portable sections use `sysinfo`/`std`; Windows-only sections have a
//! real `#[cfg(windows)]` shape and a portable `n/a` stub off Windows.

pub mod app_updates;
pub mod autostart;
pub mod av_thirdparty;
pub mod backup_status;
pub mod battery;
pub mod browser_extensions;
pub mod defender;
pub mod defender_quarantine;
pub mod disk;
pub mod disk_smart;
pub mod encryption;
pub mod firewall;
pub mod installed_software;
pub mod listening_ports;
pub mod local_accounts;
pub mod memory;
pub mod net_quality;
pub mod network;
pub mod os_support;
pub mod peripherals;
pub mod printers;
pub mod processes;
pub mod reboot_pending;
pub mod reliability;
pub mod routing;
pub mod scheduled_tasks;
pub mod screen_time;
pub mod services;
pub mod thermals;
pub mod time_sync;
pub mod uptime;
pub mod web_activity;
pub mod wifi_quality;
pub mod win_update;

/// Shared PowerShell/JSON helper used by the Windows collector bodies.
#[cfg(windows)]
pub mod winps;

use serde_json::{Map, Value};

use super::Section;
use crate::protocol::Status;

/// All section names in catalog order, paired with their collector function.
type Collector = fn() -> Section;

/// Upper bound on collectors run concurrently. Collectors are I/O-bound — each
/// Windows collector spawns a short-lived PowerShell/CIM probe — so a small pool
/// turns a cold first snapshot from "the sum of ~25 sequential probes" into "the
/// slowest probe" (each itself bounded by `winps::PROBE_BUDGET`) without spawning
/// dozens of PowerShell processes at once.
const MAX_COLLECTOR_THREADS: usize = 8;

/// Registry of `(name, collector)` covering every section in the contract.
fn registry() -> Vec<(&'static str, Collector)> {
    vec![
        // Mandatory.
        ("disk", disk::collect),
        ("peripherals", peripherals::collect),
        ("network", network::collect),
        ("routing", routing::collect),
        ("processes", processes::collect),
        ("services", services::collect),
        ("defender", defender::collect),
        ("win_update", win_update::collect),
        // Hardware health.
        ("disk_smart", disk_smart::collect),
        ("battery", battery::collect),
        ("memory", memory::collect),
        ("thermals", thermals::collect),
        // Security & crypto.
        ("firewall", firewall::collect),
        ("encryption", encryption::collect),
        ("av_thirdparty", av_thirdparty::collect),
        ("defender_quarantine", defender_quarantine::collect),
        // Update & stability.
        ("reboot_pending", reboot_pending::collect),
        ("os_support", os_support::collect),
        ("reliability", reliability::collect),
        ("app_updates", app_updates::collect),
        // Operations & daily.
        ("uptime", uptime::collect),
        ("time_sync", time_sync::collect),
        ("printers", printers::collect),
        ("wifi_quality", wifi_quality::collect),
        ("autostart", autostart::collect),
        // Parental controls.
        ("web_activity", web_activity::collect),
        ("screen_time", screen_time::collect),
        // Security inventory.
        ("installed_software", installed_software::collect),
        ("browser_extensions", browser_extensions::collect),
        ("listening_ports", listening_ports::collect),
        ("scheduled_tasks", scheduled_tasks::collect),
        ("local_accounts", local_accounts::collect),
        // Resilience.
        ("backup_status", backup_status::collect),
        ("net_quality", net_quality::collect),
    ]
}

/// Collect a snapshot. When `wanted` is non-empty, only those sections are run.
///
/// Collectors run on a bounded thread pool ([`MAX_COLLECTOR_THREADS`]) so a cold
/// snapshot completes in roughly the slowest probe's time rather than the sum of
/// every probe — without which one slow Windows CIM/PowerShell call would gate the
/// whole push. The output `Map` is a `BTreeMap`, so the result is identically
/// ordered regardless of the order collectors happen to finish.
pub fn collect_all(wanted: &[String]) -> Map<String, Value> {
    let entries: Vec<(&'static str, Collector)> = registry()
        .into_iter()
        .filter(|(name, _)| wanted.is_empty() || wanted.iter().any(|w| w == name))
        .collect();
    run_collectors(entries)
}

/// Run the given collectors on a bounded pool of threads and assemble the snapshot.
///
/// Each collector is isolated with `catch_unwind`, so a single panicking probe yields
/// a degraded (`crit`) section instead of losing the entire snapshot.
fn run_collectors(entries: Vec<(&'static str, Collector)>) -> Map<String, Value> {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;

    if entries.is_empty() {
        return Map::new();
    }

    let next = AtomicUsize::new(0);
    let (tx, rx) = mpsc::channel::<(String, Value)>();
    let workers = entries.len().min(MAX_COLLECTOR_THREADS);

    std::thread::scope(|scope| {
        for _ in 0..workers {
            let tx = tx.clone();
            let next = &next;
            let entries = &entries;
            scope.spawn(move || loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                let Some(&(name, f)) = entries.get(i) else {
                    break;
                };
                // A collector is a bare `fn` with no shared state, so it is unwind-safe.
                let value = std::panic::catch_unwind(f)
                    .map(Section::into_value)
                    .unwrap_or_else(|_| panicked_section(name));
                let _ = tx.send((name.to_string(), value));
            });
        }
        // Drop the original sender so the receiver loop ends once every worker
        // (each holding a clone) has finished.
        drop(tx);

        let mut snapshot = Map::new();
        for (name, value) in rx {
            snapshot.insert(name, value);
        }
        snapshot
    })
}

/// Degraded section emitted when a collector panics, so the snapshot still carries
/// the key with the contract-required `status`/`summary`.
fn panicked_section(name: &str) -> Value {
    Section::with_fields(
        Status::Crit,
        format!("collector {name} panicked"),
        Value::Object(Map::new()),
    )
    .into_value()
}

/// Names of all sections this agent knows how to collect.
#[allow(dead_code)] // introspection helper; exercised by tests.
pub fn section_names() -> Vec<&'static str> {
    registry().into_iter().map(|(n, _)| n).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collect_all_covers_every_section() {
        let snap = collect_all(&[]);
        assert_eq!(snap.len(), section_names().len());
        for (name, value) in &snap {
            assert!(
                value.get("status").and_then(|s| s.as_str()).is_some(),
                "section {name} missing status"
            );
            assert!(
                value.get("summary").and_then(|s| s.as_str()).is_some(),
                "section {name} missing summary"
            );
        }
    }

    #[test]
    fn collect_all_respects_section_filter() {
        let snap = collect_all(&["disk".to_string(), "memory".to_string()]);
        assert_eq!(snap.len(), 2);
        assert!(snap.contains_key("disk"));
        assert!(snap.contains_key("memory"));
    }

    #[test]
    fn run_collectors_isolates_panics_and_runs_every_entry() {
        fn good() -> Section {
            Section::with_fields(Status::Ok, "fine", serde_json::json!({"k": 1}))
        }
        fn boom() -> Section {
            panic!("collector blew up")
        }
        // Silence the default panic hook so the deliberately-panicking collector does
        // not spam test output; restore it afterwards.
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        let entries: Vec<(&'static str, Collector)> = vec![("good", good), ("boom", boom)];
        let snap = run_collectors(entries);
        std::panic::set_hook(prev);

        assert_eq!(snap.len(), 2);
        assert_eq!(snap["good"]["status"], "ok");
        // The panicking collector is isolated into a degraded section, not lost.
        assert_eq!(snap["boom"]["status"], "crit");
        assert!(snap["boom"]["summary"]
            .as_str()
            .unwrap()
            .contains("panicked"));
    }
}
