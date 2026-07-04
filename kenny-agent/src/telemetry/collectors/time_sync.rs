//! `time_sync` section — system clock synchronization state.
//!
//! Real data from `w32tm /query /status` on Windows. That command talks to the
//! *running* Windows Time service (`W32Time`) over RPC, so it fails whenever the
//! service is not currently up. On non-domain (home/family) Windows 10/11 the
//! service defaults to **Manual (Trigger Start)**: it starts on demand, syncs the
//! clock, and stops again when idle. A stopped-but-trigger-start service is the
//! normal state — not a fault — so a failed query is classified against the actual
//! service configuration instead of being reported as a blanket warning.

use serde_json::json;

use crate::telemetry::Section;
// Only the portable `#[cfg(not(windows))]` stub names `Status` at this level; the
// Windows path routes through `core`/`windows_impl`, which import it themselves.
#[cfg(not(windows))]
use crate::protocol::Status;

/// Collect the `time_sync` section.
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
            json!({ "synchronized": null, "source": null, "offset_secs": null }),
        )
    }
}

/// Portable classification core — compiled and tested on every platform.
///
/// Splitting the decision logic out of the Windows probes keeps the "is the clock
/// healthy?" rules under `cargo test` on Linux CI, where `w32tm` does not exist.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use crate::protocol::Status;

    /// Clock offset (seconds) above which we treat the skew as a warning.
    pub const MAX_OFFSET_SECS: f64 = 5.0;

    /// Fields parsed from `w32tm /query /status`.
    #[derive(Debug, Default, Clone, PartialEq)]
    pub struct QueryStatus {
        pub source: Option<String>,
        pub offset_secs: Option<f64>,
    }

    /// Parse the `key: value` lines of `w32tm /query /status` output.
    pub fn parse_query_status(raw: &str) -> QueryStatus {
        let mut qs = QueryStatus::default();
        for line in raw.lines() {
            let Some((k, v)) = line.split_once(':') else {
                continue;
            };
            let key = k.trim().to_lowercase();
            let val = v.trim();
            match key.as_str() {
                "source" if !val.is_empty() => qs.source = Some(val.to_string()),
                // e.g. "Phase Offset: 0.0123456s"
                "phase offset" => {
                    qs.offset_secs = val.trim_end_matches('s').trim().parse::<f64>().ok();
                }
                _ => {}
            }
        }
        qs
    }

    /// Whether a parse of `w32tm /query /status` actually yielded a status we can
    /// classify, as opposed to an error body (or nothing).
    ///
    /// `w32tm` prints its status to stdout even when it exits non-zero, but on failure
    /// the body is an error message with no `Source:` / `Phase Offset:` lines — which
    /// parses into an empty [`QueryStatus`]. A source or an offset means we got real
    /// data and should trust the live-status path over the service-config fallback.
    pub fn has_usable_status(qs: &QueryStatus) -> bool {
        qs.source.is_some() || qs.offset_secs.is_some()
    }

    /// A source that is a real network peer (not the fallback local hardware clock).
    pub fn is_network_synchronized(source: Option<&str>) -> bool {
        source
            .map(|s| !s.is_empty() && !s.eq_ignore_ascii_case("Local CMOS Clock"))
            .unwrap_or(false)
    }

    /// Classify a successful `w32tm /query /status`: large skew or a non-network
    /// source is a warning; otherwise the clock is healthy.
    ///
    /// Returns `(status, summary, synchronized)`.
    pub fn classify_query(qs: &QueryStatus) -> (Status, String, bool) {
        let synchronized = is_network_synchronized(qs.source.as_deref());
        let big_skew = qs
            .offset_secs
            .map(|o| o.abs() > MAX_OFFSET_SECS)
            .unwrap_or(false);

        if big_skew {
            (
                Status::Warn,
                format!("clock offset {:.2}s", qs.offset_secs.unwrap_or(0.0)),
                synchronized,
            )
        } else if !synchronized {
            (
                Status::Warn,
                "clock not network-synchronized".to_string(),
                synchronized,
            )
        } else {
            (Status::Ok, "clock synchronized".to_string(), synchronized)
        }
    }

    /// Classify when `w32tm /query /status` could not be reached, using the service's
    /// running state and start mode (`Win32_Service.State` / `.StartMode`).
    ///
    /// A trigger-/manual-/auto-start service that is merely stopped is the normal
    /// idle state on a family PC and must not warn — the clock is still kept in sync
    /// on demand. Only a disabled or missing service (or a service that is running
    /// yet still refuses the query) is a real fault.
    pub fn classify_service(state: Option<&str>, start_mode: Option<&str>) -> (Status, String) {
        let disabled = start_mode
            .map(|m| m.eq_ignore_ascii_case("Disabled"))
            .unwrap_or(false);
        let running = state
            .map(|s| s.eq_ignore_ascii_case("Running"))
            .unwrap_or(false);

        match (start_mode, disabled, running) {
            // Service not present at all.
            (None, _, _) => (Status::Warn, "time service not found".to_string()),
            // Explicitly turned off — the clock will drift.
            (_, true, _) => (Status::Warn, "time service disabled".to_string()),
            // Running but the query still failed — a genuine anomaly worth surfacing.
            (_, _, true) => (Status::Warn, "time service not responding".to_string()),
            // Stopped but eligible to trigger-start: the normal idle state.
            _ => (Status::Ok, "clock synchronized (service idle)".to_string()),
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    /// Collect `time_sync`. Prefer live status from `w32tm`; if the service is not
    /// currently up, fall back to classifying its configuration so a trigger-start
    /// service in its normal idle state does not raise a false warning.
    ///
    /// `w32tm /query /status` prints a usable status to stdout even when it exits
    /// non-zero, and the act of calling it *trigger-starts* the (Manual/Trigger-Start)
    /// Windows Time service. So we: read stdout regardless of exit code; if it parsed a
    /// real status, classify it; otherwise inspect the service config, and if the
    /// service is now `Running` (our probe just woke it) retry the query once before
    /// concluding anything. Only a service that stays unreadable while confirmed running
    /// — or one that is disabled/missing — is a genuine fault.
    pub fn collect() -> Section {
        // First attempt: trust the output, not the exit code.
        if let Some(qs) = query_w32tm() {
            return query_section(&qs);
        }

        // No usable live status — decide from the service config.
        let (state, start_mode) = query_service();
        let running = state
            .as_deref()
            .map(|s| s.eq_ignore_ascii_case("Running"))
            .unwrap_or(false);

        // The first `w32tm` call trigger-starts W32Time; if it is now running, the
        // service is up and simply was not ready a moment ago — retry once rather than
        // punish it for the wake-up we caused.
        if running {
            if let Some(qs) = query_w32tm() {
                return query_section(&qs);
            }
        }

        let (status, summary) = core::classify_service(state.as_deref(), start_mode.as_deref());
        Section::with_fields(
            status,
            summary,
            json!({
                // Unknown while the service is idle — reported as null, not a false "no".
                "synchronized": Value::Null,
                "source": Value::Null,
                "offset_secs": Value::Null,
            }),
        )
    }

    /// Run `w32tm /query /status`, returning a parsed status only when the output
    /// actually contains one (exit code ignored — see [`winps::run_command_output`]).
    fn query_w32tm() -> Option<core::QueryStatus> {
        let raw = winps::run_command_output("w32tm", &["/query", "/status"])?;
        let qs = core::parse_query_status(&raw);
        core::has_usable_status(&qs).then_some(qs)
    }

    /// Build the section from a parsed live `w32tm` status.
    fn query_section(qs: &core::QueryStatus) -> Section {
        let (status, summary, synchronized) = core::classify_query(qs);
        Section::with_fields(
            status,
            summary,
            json!({
                "synchronized": synchronized,
                "source": qs.source,
                "offset_secs": qs.offset_secs,
            }),
        )
    }

    /// Read `Win32_Service.State` / `.StartMode` for `W32Time`. Either may be `None`
    /// if the probe fails or the service is absent.
    fn query_service() -> (Option<String>, Option<String>) {
        let script = "$s = Get-CimInstance Win32_Service -Filter \"Name='W32Time'\" \
             -ErrorAction SilentlyContinue; \
             if ($s) { [pscustomobject]@{ state = [string]$s.State; \
             start = [string]$s.StartMode } | ConvertTo-Json -Compress }";
        let Some(v) = winps::run_json(script) else {
            return (None, None);
        };
        let field = |k: &str| {
            v.get(k)
                .and_then(Value::as_str)
                .map(str::to_string)
                .filter(|s| !s.is_empty())
        };
        (field("state"), field("start"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    // `Status` is only re-exported through `super` on the `#[cfg(not(windows))]` path,
    // but the tests below reference it on every platform — import it here directly.
    use crate::protocol::Status;

    #[test]
    fn time_sync_section_is_valid() {
        assert!(collect().into_value()["status"].is_string());
    }

    #[test]
    fn parses_source_and_offset() {
        let raw = "Leap Indicator: 0(no warning)\n\
                   Stratum: 3 (secondary reference - syncd by (S)NTP)\n\
                   Source: time.windows.com,0x8\n\
                   Poll Interval: 10 (1024s)\n\
                   Phase Offset: 0.0123456s\n";
        let qs = core::parse_query_status(raw);
        assert_eq!(qs.source.as_deref(), Some("time.windows.com,0x8"));
        assert_eq!(qs.offset_secs, Some(0.0123456));
    }

    #[test]
    fn synced_source_is_ok() {
        let qs = core::QueryStatus {
            source: Some("time.windows.com,0x8".into()),
            offset_secs: Some(0.01),
        };
        let (status, _, synced) = core::classify_query(&qs);
        assert_eq!(status, Status::Ok);
        assert!(synced);
    }

    #[test]
    fn local_cmos_clock_is_not_synchronized() {
        assert!(!core::is_network_synchronized(Some("Local CMOS Clock")));
        assert!(!core::is_network_synchronized(Some("")));
        assert!(!core::is_network_synchronized(None));
        assert!(core::is_network_synchronized(Some("time.windows.com,0x8")));
    }

    #[test]
    fn large_skew_warns() {
        let qs = core::QueryStatus {
            source: Some("time.windows.com,0x8".into()),
            offset_secs: Some(-42.5),
        };
        let (status, summary, _) = core::classify_query(&qs);
        assert_eq!(status, Status::Warn);
        assert!(summary.contains("42.50"));
    }

    #[test]
    fn trigger_start_idle_service_is_ok() {
        // The regression: a stopped Manual/Auto (trigger-start) service is normal on a
        // family PC and must not warn just because `w32tm` could not reach it.
        let (status, _) = core::classify_service(Some("Stopped"), Some("Manual"));
        assert_eq!(status, Status::Ok);

        let (status, _) = core::classify_service(Some("Stopped"), Some("Auto"));
        assert_eq!(status, Status::Ok);
    }

    #[test]
    fn disabled_or_missing_service_warns() {
        let (status, summary) = core::classify_service(Some("Stopped"), Some("Disabled"));
        assert_eq!(status, Status::Warn);
        assert!(summary.contains("disabled"));

        let (status, summary) = core::classify_service(None, None);
        assert_eq!(status, Status::Warn);
        assert!(summary.contains("not found"));
    }

    #[test]
    fn running_service_that_refuses_query_warns() {
        // In the collector this arm is now reached only after a retry of `w32tm` has
        // also failed while the service is confirmed running — a genuine anomaly — so
        // the warn is meaningful rather than a trigger-start race artifact. In isolation
        // the classifier still maps "running but unreadable" to a warning.
        let (status, _) = core::classify_service(Some("Running"), Some("Manual"));
        assert_eq!(status, Status::Warn);
    }

    #[test]
    fn parsed_status_is_usable_only_with_source_or_offset() {
        // A real status carries a source and/or an offset.
        let with_source = core::QueryStatus {
            source: Some("time.windows.com,0x8".into()),
            offset_secs: None,
        };
        assert!(core::has_usable_status(&with_source));

        let with_offset = core::QueryStatus {
            source: None,
            offset_secs: Some(0.01),
        };
        assert!(core::has_usable_status(&with_offset));

        // An error body from a non-zero `w32tm` exit parses into an empty status, which
        // must NOT be treated as live data (the collector falls back to the service).
        let empty = core::parse_query_status(
            "The following error occurred: The service has not been started. (0x80070426)",
        );
        assert_eq!(empty, core::QueryStatus::default());
        assert!(!core::has_usable_status(&empty));
    }

    #[test]
    fn usable_status_drives_query_classification() {
        // When `w32tm` output parses to a real network source, the live-status path is
        // used (OK) rather than the service-config fallback.
        let raw = "Source: time.windows.com,0x8\nPhase Offset: 0.0123456s\n";
        let qs = core::parse_query_status(raw);
        assert!(core::has_usable_status(&qs));
        let (status, _, synced) = core::classify_query(&qs);
        assert_eq!(status, Status::Ok);
        assert!(synced);
    }
}
