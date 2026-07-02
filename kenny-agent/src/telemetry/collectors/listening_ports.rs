//! `listening_ports` section — TCP listeners and UDP endpoints.
//!
//! Real data from `Get-NetTCPConnection -State Listen` + `Get-NetUDPEndpoint` on
//! Windows, joined pid → image name via `Get-Process`. Deduplicated by
//! `(proto, port, process)`, wildcard binds (`0.0.0.0` / `::`) first, then by
//! port. Cap 200 with a `truncated` flag; `count` is the deduplicated total
//! before the cap.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `listening_ports` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "ports": [], "count": 0, "truncated": false }),
        )
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    /// Contract cap on the `ports` list.
    pub const MAX_PORTS: usize = 200;

    /// One listening socket, as read from the probe.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Port {
        /// `tcp` or `udp`.
        pub proto: String,
        pub port: u16,
        pub address: String,
        pub pid: Option<i64>,
        pub process: Option<String>,
    }

    impl Port {
        /// Build from one probe row; rows without proto/port/address are dropped.
        pub fn from_row(row: &Value) -> Option<Port> {
            Some(Port {
                proto: row.get("proto")?.as_str()?.to_string(),
                port: u16::try_from(row.get("port")?.as_u64()?).ok()?,
                address: row.get("address")?.as_str()?.to_string(),
                pid: row.get("pid").and_then(Value::as_i64),
                process: row
                    .get("process")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(str::to_string),
            })
        }
    }

    /// True for an any-interface bind — the exposure an operator reviews first.
    pub fn is_wildcard(address: &str) -> bool {
        address == "0.0.0.0" || address == "::"
    }

    /// Sort (wildcard binds first, then port/proto/address), dedupe by
    /// `(proto, port, process)` keeping the first (wildcard-preferred) entry, cap
    /// at [`MAX_PORTS`]. Returns `(ports, count_before_cap, truncated)`.
    pub fn shape(mut ports: Vec<Port>) -> (Vec<Value>, usize, bool) {
        use std::collections::HashSet;

        ports.sort_by(|a, b| {
            is_wildcard(&b.address)
                .cmp(&is_wildcard(&a.address))
                .then_with(|| a.port.cmp(&b.port))
                .then_with(|| a.proto.cmp(&b.proto))
                .then_with(|| a.address.cmp(&b.address))
        });
        let mut seen: HashSet<(String, u16, Option<String>)> = HashSet::new();
        ports.retain(|p| seen.insert((p.proto.clone(), p.port, p.process.clone())));

        let count = ports.len();
        let truncated = count > MAX_PORTS;
        ports.truncate(MAX_PORTS);
        let out = ports
            .into_iter()
            .map(|p| {
                json!({
                    "proto": p.proto,
                    "port": p.port,
                    "address": p.address,
                    "pid": p.pid,
                    "process": p.process,
                })
            })
            .collect();
        (out, count, truncated)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn port(proto: &str, port: u16, address: &str, process: Option<&str>) -> Port {
            Port {
                proto: proto.to_string(),
                port,
                address: address.to_string(),
                pid: Some(4),
                process: process.map(str::to_string),
            }
        }

        #[test]
        fn from_row_parses_and_validates() {
            let row = json!({ "proto": "tcp", "port": 445, "address": "0.0.0.0", "pid": 4, "process": "System" });
            let p = Port::from_row(&row).unwrap();
            assert_eq!(p.proto, "tcp");
            assert_eq!(p.port, 445);
            assert_eq!(p.address, "0.0.0.0");
            assert_eq!(p.pid, Some(4));
            assert_eq!(p.process.as_deref(), Some("System"));
            // Missing process/pid stay None; out-of-range port is dropped.
            let p =
                Port::from_row(&json!({ "proto": "udp", "port": 53, "address": "::" })).unwrap();
            assert_eq!(p.pid, None);
            assert_eq!(p.process, None);
            assert!(
                Port::from_row(&json!({ "proto": "tcp", "port": 70000, "address": "::" }))
                    .is_none()
            );
            assert!(Port::from_row(&json!({ "port": 80, "address": "::" })).is_none());
        }

        #[test]
        fn shape_sorts_wildcards_first_and_dedupes() {
            let (out, count, truncated) = shape(vec![
                port("tcp", 8080, "127.0.0.1", Some("app")),
                port("tcp", 445, "0.0.0.0", Some("System")),
                // Duplicate (proto, port, process) on a specific address: the
                // wildcard bind wins the dedupe.
                port("tcp", 445, "192.168.1.5", Some("System")),
                port("udp", 53, "::", Some("dns")),
            ]);
            assert_eq!(count, 3);
            assert!(!truncated);
            // Wildcards first (port asc), then specific binds.
            assert_eq!(out[0]["port"], 53);
            assert_eq!(out[0]["address"], "::");
            assert_eq!(out[1]["port"], 445);
            assert_eq!(out[1]["address"], "0.0.0.0");
            assert_eq!(out[2]["port"], 8080);
        }

        #[test]
        fn shape_caps_at_200_and_reports_precap_count() {
            let ports: Vec<Port> = (0..220)
                .map(|i| port("tcp", 1000 + i, "0.0.0.0", Some("svc")))
                .map(|mut p| {
                    p.process = Some(format!("svc{}", p.port));
                    p
                })
                .collect();
            let (out, count, truncated) = shape(ports);
            assert_eq!(out.len(), MAX_PORTS);
            assert_eq!(count, 220);
            assert!(truncated);
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// TCP listeners + UDP endpoints joined with process names via one probe.
    pub fn collect() -> Section {
        let script = r#"
$procs = @{}
Get-Process -ErrorAction SilentlyContinue | ForEach-Object { $procs[[int]$_.Id] = [string]$_.ProcessName }
$out = @()
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
  $owner = [int]$_.OwningProcess
  $out += [pscustomobject]@{
    proto = 'tcp'; port = [int]$_.LocalPort; address = [string]$_.LocalAddress
    pid = $owner; process = $procs[$owner]
  }
}
Get-NetUDPEndpoint -ErrorAction SilentlyContinue | ForEach-Object {
  $owner = [int]$_.OwningProcess
  $out += [pscustomobject]@{
    proto = 'udp'; port = [int]$_.LocalPort; address = [string]$_.LocalAddress
    pid = $owner; process = $procs[$owner]
  }
}
ConvertTo-Json -Compress @($out)
"#;

        let rows = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();
        let ports: Vec<core::Port> = rows.iter().filter_map(core::Port::from_row).collect();
        let (ports, count, truncated) = core::shape(ports);

        Section::with_fields(
            Status::Ok,
            format!("{count} listening ports"),
            json!({ "ports": ports, "count": count, "truncated": truncated }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listening_ports_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["ports"].is_array());
        assert!(v["count"].is_number());
        assert!(v["truncated"].is_boolean());
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["ports"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
    }
}
