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
pub mod fans;
pub mod firewall;
pub mod gpu;
pub mod hardware_errors;
pub mod installed_software;
pub mod listening_ports;
pub mod local_accounts;
pub mod logon_failures;
pub mod memory;
pub mod net_quality;
pub mod network;
pub mod nvidia;
pub mod os_support;
pub mod peripherals;
pub mod printers;
pub mod proc;
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

/// Why an OS probe produced nothing usable. Collectors only need "no data" and
/// fall back to a default; an interactive tool reports the reason instead, so a
/// probe that ran out of time never reads as one that printed nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(not(windows), allow(dead_code))]
pub enum ProbeFailure {
    /// The process could not be started.
    Spawn,
    /// Still running when its budget ran out; it was killed.
    Timeout(std::time::Duration),
    /// Ran to completion with a non-zero exit code (`None`: killed by a signal).
    Exit(Option<i32>),
    /// Exited cleanly without printing anything.
    Empty,
    /// Printed something that is not the JSON the caller expected.
    Invalid,
}

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
        ("hardware_errors", hardware_errors::collect),
        ("gpu", gpu::collect),
        ("fans", fans::collect),
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
        ("logon_failures", logon_failures::collect),
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

    /// A full snapshot must finish, not merely eventually return.
    ///
    /// This is the one test that runs every collector for real, so on Windows it is
    /// also the one that meets every OS probe the agent makes. `collect_all` promises
    /// a bounded snapshot — a pool of [`MAX_COLLECTOR_THREADS`] workers, each probe
    /// held to `winps::PROBE_BUDGET` — and a collector that shells out without that
    /// bound silently breaks the promise: the run does not fail, it just never ends,
    /// taking the whole test binary with it (a `#[test]` cannot time itself out).
    /// Collecting on a detached thread and waiting on a channel turns that into a
    /// failure that names the budget it blew.
    ///
    /// The budget is the worst case the pool allows — every section queued behind a
    /// full-budget probe — with room to spare, so it can only be reached by a probe
    /// that is not bounded at all.
    const SNAPSHOT_BUDGET: std::time::Duration = std::time::Duration::from_secs(300);

    /// Run `collect_all` off-thread and fail rather than hang if it overruns
    /// [`SNAPSHOT_BUDGET`]. The thread is left running on timeout — it cannot be
    /// killed — but the process exits when the harness finishes.
    fn collect_all_within_budget(wanted: &[String]) -> Map<String, Value> {
        let (tx, rx) = std::sync::mpsc::channel();
        let wanted = wanted.to_vec();
        std::thread::spawn(move || {
            let _ = tx.send(collect_all(&wanted));
        });
        rx.recv_timeout(SNAPSHOT_BUDGET).unwrap_or_else(|_| {
            panic!(
                "collect_all did not finish within {}s: some collector runs an \
                 unbounded OS probe instead of holding to winps::PROBE_BUDGET",
                SNAPSHOT_BUDGET.as_secs()
            )
        })
    }

    #[test]
    fn collect_all_covers_every_section() {
        let snap = collect_all_within_budget(&[]);
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

    // ---- Frame budget ---------------------------------------------------------------

    /// A string of exactly `len` characters that starts with `prefix` and `i`, so every
    /// row is distinct and nothing is shorter than a realistic value.
    fn text(prefix: &str, i: usize, len: usize) -> String {
        let mut s = format!("{prefix}{i}");
        while s.len() < len {
            s.push_str("abcdefghij");
        }
        s.truncate(len);
        s
    }

    /// `n` UTC calendar-date keys with a count each.
    fn by_day(n: usize) -> Value {
        Value::Object(
            (0..n)
                .map(|d| (format!("2026-06-{:02}", d + 1), serde_json::json!(1234)))
                .collect(),
        )
    }

    fn rows(n: usize, row: impl Fn(usize) -> Value) -> Vec<Value> {
        (0..n).map(row).collect()
    }

    const TS: &str = "2026-06-04T17:40:00Z";

    /// A worst-case payload for `name`: every list filled to the cap the contract
    /// documents (`docs/protocol.md`), with strings at realistic lengths. Lists the
    /// contract does not cap are sized for a large host and say so; they are estimates,
    /// not bounds. An unknown name fails the test, so registering a section forces
    /// someone to say how big it can get.
    fn worst_case_section(name: &str) -> Value {
        use serde_json::json;
        let section = |fields: Value| {
            let mut obj = match fields {
                Value::Object(m) => m,
                _ => unreachable!("fields are an object"),
            };
            obj.insert("status".into(), json!("ok"));
            obj.insert("summary".into(), json!(text("summary ", 0, 60)));
            Value::Object(obj)
        };
        match name {
            // --- Capped by the contract ---------------------------------------------
            "web_activity" => section(json!({
                "window_hours": 24, "sources": ["dns_cache", "browser_history"],
                "domains": rows(250, |i| json!({
                    "domain": text("host", i, 25), "first_seen": TS, "last_seen": TS,
                    "hits": 4096, "sources": ["dns_cache", "browser_history"] })),
                "truncated": true, "browser_profiles_read": 12, "errors": [],
            })),
            "installed_software" => section(json!({
                "apps": rows(300, |i| json!({
                    "name": text("App ", i, 40), "version": "124.0.6367.119",
                    "publisher": text("Pub ", i, 25), "install_date": "2026-03-11" })),
                "count": 900, "truncated": true,
            })),
            "browser_extensions" => section(json!({
                "extensions": rows(200, |i| json!({
                    "browser": "chrome", "id": text("ext", i, 32),
                    "name": text("Ext ", i, 40), "version": "1.58.0" })),
                "count": 400, "truncated": true, "profiles_read": 12, "errors": [],
            })),
            "listening_ports" => section(json!({
                "ports": rows(200, |i| json!({
                    "proto": "tcp", "port": 49152 + i, "address": "0.0.0.0",
                    "pid": 10_000 + i, "process": text("proc", i, 20) })),
                "count": 400, "truncated": true,
            })),
            "scheduled_tasks" => section(json!({
                "tasks": rows(200, |i| json!({
                    "path": text("\\Vendor\\", i, 30), "name": text("Task ", i, 40),
                    "state": "Ready", "action": text("C:\\Program Files\\Vendor\\", i, 120),
                    "run_as": text("example-pc\\user", i, 25), "last_result": 267011,
                    "next_run": TS })),
                "count": 400, "total_count": 900, "truncated": true,
            })),
            "reliability" => section(json!({
                "stability_index": 6.8, "recent_crashes": 99_999, "window_days": 7,
                "events": rows(40, |i| json!({
                    "source": text("Source-", i, 30), "event_id": 10_000 + i,
                    "level": "error", "count": 99_999, "sample": text("sample ", i, 200),
                    "last_seen": TS, "by_day": by_day(7) })),
                "boot_sessions": rows(20, |_| json!(TS)),
                "truncated": true, "truncated_count": 120,
            })),
            "hardware_errors" => section(json!({
                "window_days": 14, "effective_window_days": 14,
                "oldest_event_utc": TS, "sources": ["event_log"],
                "groups": rows(24, |i| json!({
                    "source": text("Microsoft-Windows-", i, 30), "event_id": 10_000 + i,
                    "level": "warning", "count": 99_999, "last_seen": TS,
                    "by_day": by_day(14), "sample": text("sample ", i, 200),
                    // 5 keys x 5 values, as the contract allows.
                    "details": Value::Object((0..5).map(|k| (
                        text("detail_key_", k, 18),
                        Value::Object((0..5).map(|v| (text("value ", v, 24), json!(999))).collect()),
                    )).collect()) })),
                "truncated": true, "truncated_count": 40,
                "app_crashes": { "total": 99_999, "distinct_apps": 50, "distinct_modules": 50,
                    "exception_codes": Value::Object((0..8).map(|k| (format!("0xc000{k:04x}"), json!(999))).collect()),
                    "by_day": by_day(14) },
                "edac": rows(32, |i| json!({ "controller": format!("mc{i}"),
                    "ce_count": 99_999, "ue_count": 99_999 })),
                "aer": rows(32, |i| json!({ "device": format!("0000:{i:02x}:00.0"),
                    "correctable": 99_999, "nonfatal": 99_999, "fatal": 99_999 })),
                "errors": [],
            })),
            "gpu" => section(json!({
                "gpus": rows(8, |i| json!({
                    "name": text("NVIDIA GeForce RTX ", i, 40), "vendor": "nvidia",
                    "pci_id": "10de:2704", "bus_id": format!("0000:0{i}:00.0"),
                    "uuid": "GPU-4f1c2a6e-8d3b-7c59-1e20-a9b3c4d5e6f7",
                    "driver_version": "560.94", "sources": ["wmi", "nvidia-smi", "sysfs"],
                    "temperature_c": 47, "utilization_percent": 100, "power_draw_w": 338.52,
                    "power_limit_w": 320.0, "fan_target_percent": 100,
                    "pcie": { "gen_current": 4, "gen_max": 4, "width_current": 16, "width_max": 16 },
                    "throttle": { "hw_slowdown": false, "hw_thermal_slowdown": false,
                        "hw_power_brake_slowdown": false, "sw_thermal_slowdown": false },
                    "ecc": { "uncorrected_volatile": 99_999, "retired_pages_pending": false,
                        "remapped_rows": { "correctable": 99, "uncorrectable": 99,
                            "pending": 99, "failure": 99 } },
                    "ras": { "gfx": { "ue": 99_999, "ce": 99_999 }, "umc": { "ue": 99_999, "ce": 99_999 },
                        "sdma": { "ue": 99_999, "ce": 99_999 }, "mmhub": { "ue": 99_999, "ce": 99_999 } } })),
                "truncated": true, "errors": [],
            })),
            "fans" => section(json!({
                "sources_tried": ["hwmon", "lhm_wmi"], "sample_interval_ms": 1000,
                "fans": rows(16, |i| json!({
                    "key": text("nct6798.fan", i, 20), "label": text("CHA_FAN", i, 12),
                    "source": "hwmon", "rpm_samples": [11_180, 11_176, 11_182, 11_179, 11_181],
                    "duty_percent": 45.0, "mode": "pwm", "idle_or_absent": false })),
                "truncated": true, "errors": [],
            })),
            "disk_smart" => section(json!({
                "disks": rows(16, |i| json!({
                    "model": text("WD_BLACK SN850X ", i, 40), "health_status": "Healthy",
                    "predictive_failure": false, "wear": 100, "temperature_c": 41,
                    "power_on_hours": 99_999, "read_errors_total": u64::MAX,
                    "read_errors_uncorrected": u64::MAX, "write_errors_uncorrected": u64::MAX,
                    "device_number": i, "serial": text("SN", i, 20), "bus_type": "NVMe",
                    "media_type": "SSD", "size_bytes": 2_000_398_934_016_u64,
                    "removable": false, "temperature_max_c": 74,
                    "smart_attributes": { "5": u64::MAX, "187": u64::MAX, "188": u64::MAX,
                        "197": u64::MAX, "198": u64::MAX, "199": u64::MAX },
                    "nvme": { "critical_warning": 0, "available_spare": 100,
                        "available_spare_threshold": 10, "percentage_used": 100,
                        "media_errors": u64::MAX, "unsafe_shutdowns": u64::MAX,
                        "error_log_entries": u64::MAX, "data_units_written": u64::MAX,
                        "power_on_hours": u64::MAX, "temperature_c": 41 },
                    "nvme_error": text("unsupported by driver ", i, 40), "paused": false })),
            })),
            "logon_failures" => section(json!({
                "window_hours": 24,
                "accounts": rows(50, |i| json!({ "name": text("user", i, 20), "count": 99_999,
                    "types": ["interactive", "network", "remote"] })),
                "unmatched_count": 99_999, "count": 99_999, "truncated": true,
            })),
            "processes" => section(json!({
                "count": 999,
                "processes": rows(15, |i| json!({ "pid": 100_000 + i, "name": text("proc", i, 24),
                    "cpu": 12.345678, "mem_bytes": 17_179_869_184_u64 })),
            })),
            "screen_time" => section(json!({
                "window_days": 7,
                "days": rows(7, |d| json!({ "date": format!("2026-06-{:02}", d + 1), "active_minutes": 1440 })),
                "source": "eventlog", "errors": [],
            })),
            // --- Not capped by the contract: sized for a large host ------------------
            "services" => section(json!({ "services": rows(400, |i| json!({
                "name": text("Svc", i, 20), "display": text("Service display name ", i, 40),
                "status": "Running", "start": "Auto" })) })),
            "peripherals" => section(json!({ "devices": rows(600, |i| json!({
                "name": text("Device ", i, 45), "class": "USB", "status": "OK" })) })),
            "routing" => section(json!({
                "routes": rows(200, |i| json!({ "destination": "192.168.100.200/32",
                    "next_hop": "192.168.100.254", "interface": text("Ethernet ", i, 20),
                    "metric": 4_281 })),
                "default_interface": "Ethernet 0",
            })),
            "network" => section(json!({
                "interfaces": rows(24, |i| json!({ "name": text("Ethernet ", i, 20),
                    "mac": "00:11:22:33:44:55",
                    "ips": ["192.168.100.200", "fe80::1c2b:3d4e:5f60:7182"] })),
                "dns": rows(6, |_| json!("192.168.100.254")),
            })),
            "autostart" => section(json!({ "entries": rows(80, |i| json!({
                "name": text("Entry ", i, 30), "command": text("\"C:\\Program Files\\Vendor\\", i, 120),
                "location": "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run" })) })),
            "app_updates" => section(json!({ "available": 60, "packages": rows(60, |i| json!({
                "id": text("Vendor.Package", i, 30), "name": text("Package ", i, 40),
                "version": "124.0.6367.119", "available": "125.0.6422.60" })) })),
            "printers" => section(json!({ "printers": rows(20, |i| json!({
                "name": text("Printer ", i, 40), "default": false, "status": "Normal" })) })),
            "defender_quarantine" => section(json!({ "items": rows(50, |i| json!({
                "name": text("Trojan:Win32/Threat", i, 40), "severity": "5", "detected_at": TS })) })),
            "av_thirdparty" => section(json!({ "products": rows(5, |i| json!({
                "name": text("Antivirus ", i, 30), "state": "enabled", "up_to_date": true })) })),
            "firewall" => section(json!({ "profiles": rows(3, |i| json!({
                "name": text("Profile", i, 8), "enabled": true })) })),
            "encryption" => section(json!({ "volumes": rows(26, |i| json!({
                "mount": format!("{}:", (b'A' + i as u8) as char),
                "protection_status": 1, "encryption_percent": 100 })) })),
            "disk" => section(json!({
                "volumes": rows(26, |i| json!({ "mount": format!("{}:", (b'A' + i as u8) as char),
                    "total_bytes": 511_000_000_000_u64, "free_bytes": 46_000_000_000_u64,
                    "percent_used": 91 })),
                "top_dirs": rows(10, |i| json!({ "path": text("C:\\Users\\testuser\\", i, 60),
                    "bytes": 120_000_000_000_u64 })),
            })),
            "thermals" => section(json!({ "sensors": rows(64, |i| json!({
                "label": text("Sensor ", i, 30), "temperature_c": 61.5 })) })),
            "win_update" => section(json!({ "last_check": TS, "recent": rows(30, |i| json!({
                "kb": format!("KB5037{i:03}"), "title": text("2026-05 Cumulative Update ", i, 60),
                "result": "succeeded", "installed_at": TS })) })),
            "local_accounts" => section(json!({
                "accounts": rows(50, |i| json!({
                    "name": text("user", i, 20), "display": text("Display Name ", i, 30),
                    "kind": "microsoft", "enabled": true, "is_admin": false,
                    "password_required": true, "password_last_set": TS, "last_logon": TS,
                    "builtin_admin": false, "builtin_guest": false,
                    "deny_logon": ["network", "remote_interactive"],
                    "unsupported": { "reset_password": "password_in_cloud" } })),
                "admins": rows(10, |i| json!(text("admin", i, 20))), "count": 50,
                "password_policy": { "applies_to": "local_only", "min_length": 8,
                    "max_age_days": 0, "lockout_threshold": 10 },
            })),
            // --- Scalar sections: one small object -----------------------------------
            "defender" => section(json!({ "enabled": true, "realtime_protection": true,
                "last_scan": TS, "last_scan_type": "quick", "last_signature_update": TS,
                "threats_found": 0, "action_needed": false })),
            "battery" => section(json!({ "present": true, "charge_percent": 100,
                "health_percent": 99.5, "status": "Charging" })),
            "memory" => section(json!({ "total_bytes": 68_719_476_736_u64,
                "used_bytes": 34_359_738_368_u64, "available_bytes": 34_359_738_368_u64,
                "percent_used": 50, "swap_total_bytes": 17_179_869_184_u64,
                "swap_used_bytes": 1_073_741_824_u64 })),
            "reboot_pending" => section(json!({ "pending": true,
                "reasons": ["WindowsUpdate", "ComponentServicing", "PendingFileRename"] })),
            "os_support" => section(json!({ "name": "Windows", "version": "11 (26200)",
                "build": "26200", "eol": false, "eol_date": null, "arch": "x86_64",
                "channel": "stable", "cpu": { "vendor": "GenuineIntel",
                    "brand": text("13th Gen Intel(R) Core(TM) ", 0, 50), "family": 6,
                    "model": 183, "stepping": 1, "microcode": "0x12b",
                    "microcode_bios": "0x123" } })),
            "uptime" => {
                section(json!({ "uptime_secs": 273_600, "boot_time_unix": 1_780_322_400_u64 }))
            }
            "time_sync" => section(json!({ "synchronized": true,
                "source": "time.windows.com,0x9", "offset_secs": 0.0012 })),
            "wifi_quality" => section(json!({ "connected": true,
                "ssid": text("network", 0, 32), "signal_percent": 80, "band": "5 GHz",
                "rssi_dbm": -52 })),
            "backup_status" => section(json!({
                "restore_points": { "enabled": true, "count": 5, "latest": TS },
                "file_history": { "service_state": "stopped", "configured": null },
                "onedrive": { "installed": true, "running": true } })),
            "net_quality" => section(json!({
                "gateway": { "host": "192.168.100.254", "latency_ms": 2.0, "loss_percent": 0 },
                "reference": { "host": "1.1.1.1", "latency_ms": 14.0, "loss_percent": 0 },
                "samples": 5, "errors": [] })),
            other => panic!(
                "no worst-case payload for section {other}: add one to worst_case_section \
                 (fill every list to its documented cap) so the frame-budget test covers it"
            ),
        }
    }

    /// The `telemetry` frame as it goes on the wire.
    fn frame_bytes(snapshot: Map<String, Value>) -> usize {
        let frame = crate::protocol::Frame::Telemetry(crate::protocol::Telemetry {
            agent_id: "11111111-2222-4333-8444-555555555555".to_string(),
            collected_at: "2026-06-04T17:40:00Z".to_string(),
            snapshot,
        });
        serde_json::to_vec(&frame).expect("serialize frame").len()
    }

    /// Per-section serialized sizes, largest first, for the failure message.
    fn size_report(snapshot: &Map<String, Value>) -> String {
        let mut sizes: Vec<(usize, &String)> = snapshot
            .iter()
            .map(|(name, v)| (v.to_string().len(), name))
            .collect();
        sizes.sort_by(|a, b| b.cmp(a));
        sizes
            .iter()
            .map(|(n, name)| format!("{name}={n}"))
            .collect::<Vec<_>>()
            .join(", ")
    }

    /// The sections that carry hardware-health facts, and the share of the frame their
    /// worst case may take. The server drops an unsolicited `telemetry` frame larger than
    /// 256 KiB (`_MAX_TELEMETRY_BYTES` in `kenny_server/tunnel.py`) and only logs a
    /// warning, so the agent bounds what these sections may add to it. Measured at
    /// ~65 KiB; the rest of the frame belongs to the older inventory sections.
    const HARDWARE_SECTIONS: [&str; 4] = ["hardware_errors", "gpu", "fans", "disk_smart"];
    const HARDWARE_SECTIONS_BUDGET_BYTES: usize = 80 * 1024;

    fn worst_case_snapshot(names: &[&str]) -> Map<String, Value> {
        names
            .iter()
            .map(|name| (name.to_string(), worst_case_section(name)))
            .collect()
    }

    /// A section added to the registry without a worst-case payload fails here (and in
    /// `worst_case_section`'s panic message) instead of silently escaping the budget.
    #[test]
    fn every_registered_section_has_a_worst_case_payload() {
        let snapshot = worst_case_snapshot(&section_names());
        assert_eq!(snapshot.len(), section_names().len());
        for (name, value) in &snapshot {
            assert!(value["status"].is_string(), "{name}: status");
            assert!(value["summary"].is_string(), "{name}: summary");
        }
    }

    #[test]
    fn the_hardware_sections_at_their_caps_fit_their_share_of_the_frame() {
        let snapshot = worst_case_snapshot(&HARDWARE_SECTIONS);
        let total = frame_bytes(snapshot.clone());
        assert!(
            total < HARDWARE_SECTIONS_BUDGET_BYTES,
            "hardware sections take {total} bytes at their caps, over the \
             {HARDWARE_SECTIONS_BUDGET_BYTES} share; per section: {}",
            size_report(&snapshot)
        );
    }
}
