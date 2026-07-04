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
    #[cfg(target_os = "linux")]
    {
        linux_impl::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::*;
    use std::net::{Ipv4Addr, Ipv6Addr};

    /// Decode a hex string into its raw bytes (even length required).
    fn hex_bytes(hex: &str) -> Option<Vec<u8>> {
        if !hex.len().is_multiple_of(2) {
            return None;
        }
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).ok())
            .collect()
    }

    /// Decode a `/proc/net/tcp` v4 address (little-endian hex) to dotted-quad.
    fn decode_v4(hex: &str) -> Option<String> {
        let mut bytes = hex_bytes(hex)?;
        if bytes.len() != 4 {
            return None;
        }
        bytes.reverse();
        Some(Ipv4Addr::new(bytes[0], bytes[1], bytes[2], bytes[3]).to_string())
    }

    /// Decode a `/proc/net/tcp6` v6 address (four little-endian 32-bit words),
    /// best-effort, to a normal IPv6 string.
    fn decode_v6(hex: &str) -> Option<String> {
        let bytes = hex_bytes(hex)?;
        if bytes.len() != 16 {
            return None;
        }
        let mut out = [0u8; 16];
        for word in 0..4 {
            for i in 0..4 {
                out[word * 4 + i] = bytes[word * 4 + (3 - i)];
            }
        }
        Some(Ipv6Addr::from(out).to_string())
    }

    /// Parse one `/proc/net/{tcp,udp}[6]` table into ports.
    ///
    /// The header line is skipped. Column 2 is `local_address` as `ADDR:PORT`
    /// (hex, little-endian addr); column 4 is `st` (hex socket state). For TCP
    /// only `0A` (LISTEN) is kept; every UDP row is kept. inode→pid mapping is
    /// out of scope, so `pid`/`process` stay `None`.
    fn parse_proc_net(raw: &str, proto: &str, is_v6: bool) -> Vec<core::Port> {
        let listen_only = proto == "tcp";
        raw.lines()
            .skip(1)
            .filter_map(|line| {
                let mut cols = line.split_whitespace();
                let _sl = cols.next()?;
                let local = cols.next()?;
                let _rem = cols.next()?;
                let st = cols.next()?;
                if listen_only && st != "0A" {
                    return None;
                }
                let (addr_hex, port_hex) = local.split_once(':')?;
                let port = u16::from_str_radix(port_hex, 16).ok()?;
                let address = if is_v6 {
                    decode_v6(addr_hex)?
                } else {
                    decode_v4(addr_hex)?
                };
                Some(core::Port {
                    proto: proto.to_string(),
                    port,
                    address,
                    pid: None,
                    process: None,
                })
            })
            .collect()
    }

    /// Read the four `/proc/net` socket tables and shape them.
    pub fn collect() -> Section {
        let Ok(tcp4) = std::fs::read_to_string("/proc/net/tcp") else {
            return Section::with_fields(
                Status::Ok,
                "n/a on this platform",
                json!({ "ports": [], "count": 0, "truncated": false }),
            );
        };
        let mut ports = parse_proc_net(&tcp4, "tcp", false);
        if let Ok(raw) = std::fs::read_to_string("/proc/net/tcp6") {
            ports.extend(parse_proc_net(&raw, "tcp", true));
        }
        if let Ok(raw) = std::fs::read_to_string("/proc/net/udp") {
            ports.extend(parse_proc_net(&raw, "udp", false));
        }
        if let Ok(raw) = std::fs::read_to_string("/proc/net/udp6") {
            ports.extend(parse_proc_net(&raw, "udp", true));
        }
        let (ports, count, truncated) = core::shape(ports);

        Section::with_fields(
            Status::Ok,
            format!("{count} listening ports"),
            json!({ "ports": ports, "count": count, "truncated": truncated }),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn decode_addresses() {
            assert_eq!(decode_v4("0100007F").as_deref(), Some("127.0.0.1"));
            assert_eq!(decode_v4("00000000").as_deref(), Some("0.0.0.0"));
            // ::1 in /proc/net/tcp6 word layout.
            assert_eq!(
                decode_v6("00000000000000000000000001000000").as_deref(),
                Some("::1")
            );
            assert_eq!(
                decode_v6("00000000000000000000000000000000").as_deref(),
                Some("::")
            );
        }

        #[test]
        fn parse_proc_net_filters_tcp_listen_and_keeps_udp() {
            // sl local_address rem_address st ...
            let tcp = "\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 0000 100
   1: 0100007F:1538 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 67890 1 0000 20
";
            let ports = parse_proc_net(tcp, "tcp", false);
            assert_eq!(ports.len(), 1, "only the LISTEN (0A) row is kept");
            assert_eq!(ports[0].proto, "tcp");
            assert_eq!(ports[0].port, 0x16);
            assert_eq!(ports[0].address, "0.0.0.0");
            assert_eq!(ports[0].pid, None);
            assert_eq!(ports[0].process, None);

            let udp = "\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 111 2 0000 0
";
            let ports = parse_proc_net(udp, "udp", false);
            assert_eq!(ports.len(), 1, "every UDP row is kept");
            assert_eq!(ports[0].proto, "udp");
            assert_eq!(ports[0].port, 0x35);
            assert_eq!(ports[0].address, "127.0.0.1");
        }
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

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_reports_real_ports() {
        // The Linux arm reads /proc/net; assert the documented shape without
        // pinning a machine-specific count.
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["summary"].is_string());
        assert!(v["ports"].is_array());
        assert!(v["count"].is_number());
        assert!(v["truncated"].is_boolean());
    }

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["ports"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
    }
}
