//! Telemetry collectors — one module per section (see `../docs/protocol.md`).
//!
//! Mandatory sections (`disk`, `peripherals`, `network`, `routing`, `processes`,
//! `services`, `defender`, `win_update`) plus hardware/security/update/operations
//! sections. Portable sections use `sysinfo`/`std`; Windows-only sections have a
//! real `#[cfg(windows)]` shape and a portable `n/a` stub off Windows.

pub mod app_updates;
pub mod autostart;
pub mod av_thirdparty;
pub mod battery;
pub mod defender;
pub mod defender_quarantine;
pub mod disk;
pub mod disk_smart;
pub mod encryption;
pub mod firewall;
pub mod memory;
pub mod network;
pub mod os_support;
pub mod peripherals;
pub mod printers;
pub mod processes;
pub mod reboot_pending;
pub mod reliability;
pub mod routing;
pub mod services;
pub mod thermals;
pub mod time_sync;
pub mod uptime;
pub mod wifi_quality;
pub mod win_update;

use serde_json::{Map, Value};

use super::Section;

/// All section names in catalog order, paired with their collector function.
type Collector = fn() -> Section;

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
    ]
}

/// Collect a snapshot. When `wanted` is non-empty, only those sections are run.
pub fn collect_all(wanted: &[String]) -> Map<String, Value> {
    let mut snapshot = Map::new();
    for (name, f) in registry() {
        if !wanted.is_empty() && !wanted.iter().any(|w| w == name) {
            continue;
        }
        snapshot.insert(name.to_string(), f().into_value());
    }
    snapshot
}

/// Names of all sections this agent knows how to collect.
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
}
