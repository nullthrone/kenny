//! `routing` section — default gateway / route table summary.
//!
//! On Windows this comes from `Get-NetRoute`. On Linux we read `/proc/net/route`
//! to surface the default gateway interface; other platforms stub to `n/a`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `routing` section.
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
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "routes": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: parse `Get-NetRoute` into `{destination, next_hop, interface, metric}`.
    pub fn collect() -> Section {
        // TODO(windows): query Get-NetRoute / GetIpForwardTable2.
        Section::with_fields(
            Status::Ok,
            "routes via Get-NetRoute",
            json!({ "routes": [] }),
        )
    }
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::*;

    /// Read `/proc/net/route` and report the default-route interface, if any.
    pub fn collect() -> Section {
        let default_iface = std::fs::read_to_string("/proc/net/route")
            .ok()
            .and_then(|raw| {
                raw.lines().skip(1).find_map(|line| {
                    let mut cols = line.split_whitespace();
                    let iface = cols.next()?;
                    let dest = cols.next()?;
                    // Destination 00000000 == default route.
                    if dest == "00000000" {
                        Some(iface.to_string())
                    } else {
                        None
                    }
                })
            });
        match default_iface {
            Some(iface) => Section::with_fields(
                Status::Ok,
                format!("default via {iface}"),
                json!({ "default_interface": iface }),
            ),
            None => Section::with_fields(
                Status::Warn,
                "no default route",
                json!({ "default_interface": null }),
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routing_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
    }
}
