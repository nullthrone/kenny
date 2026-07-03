//! `net_quality` section — stateless link-quality probe at collection time.
//!
//! On Windows: the default gateway from `Get-NetRoute -DestinationPrefix 0.0.0.0/0`
//! (lowest-metric first hop), then 5 ICMP echoes each to the gateway and to a
//! reference host via `Test-Connection -Count 5` (per-ping results; both the
//! PS 5.1 `ResponseTime` and PS 7 `Latency` shapes are handled). `latency_ms` is
//! the median of successful echoes and `null` at 100 % loss. The reference host
//! defaults to `1.1.1.1` and can be overridden with the `KENNY_NET_QUALITY_REF_HOST`
//! environment variable.
//!
//! Gateway and reference are probed by two separate PowerShell invocations so each
//! stays well inside the per-probe budget (`winps::PROBE_BUDGET`) even when the
//! other target is unreachable.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `net_quality` section.
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
            json!({
                "gateway": { "host": null, "latency_ms": null, "loss_percent": null },
                "reference": { "host": null, "latency_ms": null, "loss_percent": null },
                "samples": 0,
                "errors": [],
            }),
        )
    }
}

/// Portable math/config core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    /// Echoes sent per target (contract: `samples`).
    pub const SAMPLES: u32 = 5;

    /// Default reference host when the override is unset or invalid.
    pub const DEFAULT_REF_HOST: &str = "1.1.1.1";

    /// Reference host: `KENNY_NET_QUALITY_REF_HOST` (validated) or the default.
    ///
    /// Read with `std::env::var` at collect time: collectors have no handle on the
    /// clap config, and the agent's other knobs are environment-driven too (every
    /// flag in `config.rs` has an `env = "KENNY_*"` binding).
    pub fn ref_host() -> String {
        match std::env::var("KENNY_NET_QUALITY_REF_HOST") {
            Ok(h) if is_valid_host(h.trim()) => h.trim().to_string(),
            _ => DEFAULT_REF_HOST.to_string(),
        }
    }

    /// Conservative host/IP charset check — the value is spliced into a PowerShell
    /// script, so anything else falls back to the default.
    pub fn is_valid_host(h: &str) -> bool {
        !h.is_empty()
            && h.len() <= 253
            && h.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | ':' | '-' | '_'))
    }

    /// Median of the successful latencies; `None` when all echoes were lost.
    pub fn median(latencies: &[f64]) -> Option<f64> {
        if latencies.is_empty() {
            return None;
        }
        let mut sorted = latencies.to_vec();
        sorted.sort_by(f64::total_cmp);
        let mid = sorted.len() / 2;
        Some(if sorted.len() % 2 == 1 {
            sorted[mid]
        } else {
            (sorted[mid - 1] + sorted[mid]) / 2.0
        })
    }

    /// Percentage of lost echoes, given the number of successful replies.
    pub fn loss_percent(successes: usize, samples: u32) -> u32 {
        let samples = samples.max(1) as usize;
        let lost = samples.saturating_sub(successes);
        (lost * 100 / samples) as u32
    }

    /// Fixture-style summary, e.g. `gateway 2ms, internet 14ms`.
    pub fn summarize(gateway_latency: Option<f64>, reference_latency: Option<f64>) -> String {
        fn part(label: &str, latency: Option<f64>) -> String {
            match latency {
                Some(l) => format!("{label} {}ms", l.round() as i64),
                None => format!("{label} unreachable"),
            }
        }
        format!(
            "{}, {}",
            part("gateway", gateway_latency),
            part("internet", reference_latency)
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn median_handles_odd_even_and_empty() {
            assert_eq!(median(&[]), None);
            assert_eq!(median(&[14.0]), Some(14.0));
            assert_eq!(median(&[3.0, 1.0, 2.0]), Some(2.0));
            assert_eq!(median(&[4.0, 1.0, 2.0, 3.0]), Some(2.5));
        }

        #[test]
        fn loss_percent_counts_lost_echoes() {
            assert_eq!(loss_percent(5, 5), 0);
            assert_eq!(loss_percent(4, 5), 20);
            assert_eq!(loss_percent(0, 5), 100);
            assert_eq!(loss_percent(7, 5), 0, "over-count clamps to no loss");
        }

        #[test]
        fn summarize_reports_medians_or_unreachable() {
            assert_eq!(
                summarize(Some(2.0), Some(14.4)),
                "gateway 2ms, internet 14ms"
            );
            assert_eq!(
                summarize(None, Some(20.0)),
                "gateway unreachable, internet 20ms"
            );
            assert_eq!(
                summarize(None, None),
                "gateway unreachable, internet unreachable"
            );
        }

        #[test]
        fn is_valid_host_rejects_script_metacharacters() {
            assert!(is_valid_host("1.1.1.1"));
            assert!(is_valid_host("ping.example-host.net"));
            assert!(is_valid_host("2606:4700:4700::1111"));
            assert!(!is_valid_host(""));
            assert!(!is_valid_host("host'; Remove-Item x"));
            assert!(!is_valid_host("host name"));
            assert!(!is_valid_host("$(evil)"));
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    /// Shared per-target ping function: 5 echoes, per-ping results, tolerant of
    /// both the PS 5.1 (`Win32_PingStatus.ResponseTime`/`StatusCode`) and PS 7
    /// (`Latency`/`Status`) reply shapes. Failed echoes simply yield no latency.
    const PING_FN: &str = r#"
function Get-KennyPingLatencies($target) {
  $lat = @()
  if ($target) {
    try {
      foreach ($r in @(Test-Connection -ComputerName $target -Count 5 -ErrorAction SilentlyContinue)) {
        if ($null -eq $r) { continue }
        if ($r.PSObject.Properties['StatusCode'] -and $null -ne $r.StatusCode -and $r.StatusCode -ne 0) { continue }
        if ($r.PSObject.Properties['Status'] -and "$($r.Status)" -ne '' -and "$($r.Status)" -ne 'Success') { continue }
        if ($r.PSObject.Properties['ResponseTime'] -and $null -ne $r.ResponseTime) { $lat += [double]$r.ResponseTime }
        elseif ($r.PSObject.Properties['Latency'] -and $null -ne $r.Latency) { $lat += [double]$r.Latency }
      }
    } catch {}
  }
  return ,@($lat)
}
"#;

    pub fn collect() -> Section {
        let mut errors: Vec<String> = Vec::new();

        // Probe 1: discover the default gateway and ping it.
        let gateway_script = format!(
            r#"{PING_FN}
$gw = $null
try {{
  $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
    Sort-Object -Property RouteMetric | Select-Object -First 1
  if ($route) {{ $gw = [string]$route.NextHop }}
}} catch {{}}
$lat = Get-KennyPingLatencies $gw
[pscustomobject]@{{ host = $gw; latencies = @($lat) }} | ConvertTo-Json -Compress"#
        );
        let gateway = probe(&gateway_script, "gateway", &mut errors);

        // Probe 2: ping the reference host. `ref_host()` is charset-validated, so
        // splicing it into the script is safe.
        let ref_host = core::ref_host();
        let reference_script = format!(
            r#"{PING_FN}
$lat = Get-KennyPingLatencies '{ref_host}'
[pscustomobject]@{{ host = '{ref_host}'; latencies = @($lat) }} | ConvertTo-Json -Compress"#
        );
        let reference = probe(&reference_script, "reference", &mut errors);

        let summary = core::summarize(
            gateway.get("latency_ms").and_then(Value::as_f64),
            reference.get("latency_ms").and_then(Value::as_f64),
        );

        Section::with_fields(
            Status::Ok,
            summary,
            json!({
                "gateway": gateway,
                "reference": reference,
                "samples": core::SAMPLES,
                "errors": errors,
            }),
        )
    }

    /// Run one ping probe script and reduce it to `{host, latency_ms, loss_percent}`.
    fn probe(script: &str, label: &str, errors: &mut Vec<String>) -> Value {
        let Some(v) = winps::run_json(script) else {
            errors.push(format!("{label} probe failed"));
            return json!({ "host": null, "latency_ms": null, "loss_percent": null });
        };
        let host = v.get("host").and_then(Value::as_str).map(str::to_string);
        if host.is_none() {
            errors.push(format!("{label}: no target host"));
            return json!({ "host": null, "latency_ms": null, "loss_percent": null });
        }
        let latencies: Vec<f64> = v
            .get("latencies")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default()
            .iter()
            .filter_map(Value::as_f64)
            .collect();
        json!({
            "host": host,
            "latency_ms": core::median(&latencies),
            "loss_percent": core::loss_percent(latencies.len(), core::SAMPLES),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn net_quality_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["samples"].is_number());
        assert!(v["errors"].is_array());
        for probe in ["gateway", "reference"] {
            assert!(v[probe].is_object());
            for key in ["host", "latency_ms", "loss_percent"] {
                assert!(v[probe].get(key).is_some(), "{probe} missing {key}");
            }
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert!(v["gateway"]["host"].is_null());
        assert!(v["reference"]["latency_ms"].is_null());
        assert_eq!(v["samples"], 0);
        assert_eq!(v["errors"].as_array().unwrap().len(), 0);
    }
}
