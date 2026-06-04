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
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    /// Parse `Get-NetRoute` into `{destination, next_hop, interface, metric}` and
    /// surface the default-route interface; `warn` when no default route exists.
    pub fn collect() -> Section {
        let script = r#"
Get-NetRoute -ErrorAction SilentlyContinue | ForEach-Object {
  $alias = $null
  try { $alias = (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction Stop).Name } catch {}
  [pscustomobject]@{
    destination = [string]$_.DestinationPrefix
    next_hop    = [string]$_.NextHop
    interface   = if ($alias) { $alias } else { [string]$_.InterfaceIndex }
    metric      = [int]$_.RouteMetric
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Warn,
                "routes unavailable",
                json!({ "routes": [], "default_interface": null }),
            );
        };
        let routes = winps::as_array(v);

        let default_iface = routes
            .iter()
            .find(|r| r.get("destination").and_then(Value::as_str) == Some("0.0.0.0/0"))
            .and_then(|r| r.get("interface").and_then(Value::as_str))
            .map(str::to_string);

        match default_iface {
            Some(iface) => Section::with_fields(
                Status::Ok,
                format!("default via {iface}"),
                json!({ "routes": routes, "default_interface": iface }),
            ),
            None => Section::with_fields(
                Status::Warn,
                "no default route",
                json!({ "routes": routes, "default_interface": null }),
            ),
        }
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
