//! `network` section — interface inventory. Portable basics via `sysinfo`.
//!
//! On Windows a richer view (DNS servers, gateways) would come from CIM; the
//! portable path lists interfaces, MACs, and IPs which is enough for fleet health.

use serde_json::json;
use sysinfo::Networks;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `network` section.
pub fn collect() -> Section {
    let networks = Networks::new_with_refreshed_list();
    let mut interfaces = Vec::new();
    for (name, data) in networks.list() {
        let mac = data.mac_address();
        let ips: Vec<String> = data
            .ip_networks()
            .iter()
            .map(|n| format!("{}/{}", n.addr, n.prefix))
            .collect();
        interfaces.push(json!({
            "name": name,
            "mac": mac.to_string(),
            "ips": ips,
        }));
    }
    let count = interfaces.len();
    let status = if count == 0 { Status::Warn } else { Status::Ok };
    Section::with_fields(
        status,
        format!("{count} interfaces"),
        json!({ "interfaces": interfaces, "dns": [] }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn network_section_lists_interfaces() {
        let v = collect().into_value();
        assert!(v["interfaces"].is_array());
        assert!(v["status"].is_string());
    }
}
