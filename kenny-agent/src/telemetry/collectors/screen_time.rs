//! `screen_time` section — whole-machine interactive minutes per calendar day.
//!
//! On Windows the last 7 days of Winlogon logon/logoff notifications (System log,
//! provider `Microsoft-Windows-Winlogon`, event ids 7001/7002) are paired
//! chronologically; an unmatched trailing logon counts until now. The window is
//! recomputed statelessly on every push — the server's daily history provides
//! longer trends.
//!
//! **Privacy (ADR-0032, hard rule):** no usernames, no per-user split, no app
//! names, and no timestamps finer than the local-calendar-day bucket ever reach
//! the payload; each day is clamped to [0, 1440]. The agent always reports
//! `status: "ok"` — kenny reports, parents judge.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Reporting window in days (contract: `window_days`).
const WINDOW_DAYS: u32 = 7;

/// Collect the `screen_time` section.
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
                "window_days": WINDOW_DAYS,
                "days": [],
                "source": "eventlog",
                "errors": [],
            }),
        )
    }
}

/// Portable pairing/summing core — compiled and tested on every platform.
///
/// Works on synthetic `(unix_seconds, Kind)` events; day bucketing takes an
/// explicit UTC offset so tests are timezone-independent.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    const DAY_SECS: i64 = 86_400;

    /// A Winlogon session-boundary event kind.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Kind {
        /// Event id 7001 — user logon notification.
        Logon,
        /// Event id 7002 — user logoff notification.
        Logoff,
    }

    /// Pair logon/logoff events chronologically into interactive intervals.
    ///
    /// A depth counter handles overlapping sessions (fast user switching) without
    /// double-counting machine time: the interval opens when depth goes 0 → 1 and
    /// closes when it returns to 0. A logoff with no matching logon (session began
    /// before the window) is dropped — its start is unknown. An unmatched trailing
    /// logon counts until `now_unix`.
    pub fn intervals_from_events(mut events: Vec<(i64, Kind)>, now_unix: i64) -> Vec<(i64, i64)> {
        events.sort_by_key(|&(t, _)| t);
        let mut intervals = Vec::new();
        let mut depth: u32 = 0;
        let mut start = 0i64;
        for (t, kind) in events {
            let t = t.min(now_unix);
            match kind {
                Kind::Logon => {
                    if depth == 0 {
                        start = t;
                    }
                    depth += 1;
                }
                Kind::Logoff => {
                    if depth > 0 {
                        depth -= 1;
                        if depth == 0 && t > start {
                            intervals.push((start, t));
                        }
                    }
                }
            }
        }
        if depth > 0 && now_unix > start {
            intervals.push((start, now_unix));
        }
        intervals
    }

    /// Sum interval seconds into minutes per **local** calendar day
    /// (`offset_secs` = local UTC offset), splitting intervals at local
    /// midnights. Days come out sorted ascending, each clamped to [0, 1440].
    pub fn minutes_per_day(intervals: &[(i64, i64)], offset_secs: i32) -> Vec<(String, u32)> {
        use std::collections::BTreeMap;

        let mut per_day: BTreeMap<String, i64> = BTreeMap::new();
        for &(start, end) in intervals {
            let mut cur = start;
            while cur < end {
                let local = cur + i64::from(offset_secs);
                // Next local midnight, expressed back in UTC seconds.
                let next_midnight =
                    (local.div_euclid(DAY_SECS) + 1) * DAY_SECS - i64::from(offset_secs);
                let chunk_end = end.min(next_midnight);
                *per_day.entry(local_date(cur, offset_secs)).or_insert(0) += chunk_end - cur;
                cur = chunk_end;
            }
        }
        per_day
            .into_iter()
            .map(|(date, secs)| (date, (secs / 60).clamp(0, 1440) as u32))
            .collect()
    }

    /// The local calendar date (`yyyy-MM-dd`) of a UTC instant at `offset_secs`.
    pub fn local_date(unix_secs: i64, offset_secs: i32) -> String {
        // Shifting by the offset and formatting as UTC yields the local date.
        chrono::DateTime::from_timestamp(unix_secs + i64::from(offset_secs), 0)
            .unwrap_or_else(|| chrono::DateTime::from_timestamp(0, 0).expect("epoch is valid"))
            .format("%Y-%m-%d")
            .to_string()
    }

    /// Fixture-style summary, e.g. `3.4h today, 24.1h over 7 days`.
    pub fn summarize(days: &[(String, u32)], today: &str) -> String {
        let today_min = days
            .iter()
            .find(|(d, _)| d == today)
            .map(|&(_, m)| m)
            .unwrap_or(0);
        let total_min: u64 = days.iter().map(|&(_, m)| u64::from(m)).sum();
        format!(
            "{:.1}h today, {:.1}h over 7 days",
            f64::from(today_min) / 60.0,
            total_min as f64 / 60.0
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn pairs_logon_logoff_chronologically() {
            let events = vec![
                (1_000, Kind::Logon),
                (4_000, Kind::Logoff),
                (10_000, Kind::Logon),
                (11_000, Kind::Logoff),
            ];
            assert_eq!(
                intervals_from_events(events, 20_000),
                vec![(1_000, 4_000), (10_000, 11_000)]
            );
        }

        #[test]
        fn overlapping_sessions_do_not_double_count() {
            // Two users logged on concurrently (fast user switching): one interval.
            let events = vec![
                (1_000, Kind::Logon),
                (2_000, Kind::Logon),
                (3_000, Kind::Logoff),
                (5_000, Kind::Logoff),
            ];
            assert_eq!(intervals_from_events(events, 20_000), vec![(1_000, 5_000)]);
        }

        #[test]
        fn trailing_logon_counts_until_now_and_leading_logoff_is_dropped() {
            // A logoff whose logon predates the window has no known start.
            let events = vec![(500, Kind::Logoff), (1_000, Kind::Logon)];
            assert_eq!(intervals_from_events(events, 3_400), vec![(1_000, 3_400)]);
            // No events at all => no intervals.
            assert!(intervals_from_events(Vec::new(), 3_400).is_empty());
        }

        #[test]
        fn minutes_per_day_splits_at_local_midnight() {
            // UTC+2: local midnight is 22:00 UTC. Interval 23:00 (day 1, local) to
            // 01:00 UTC (= 03:00 local, day 2): 1h before local midnight of day 2?
            // Take a concrete case: 2020-09-13 (unix 1_600_000_000 is 12:26:40Z).
            let day_start_utc = 1_600_000_000 - (1_600_000_000 % 86_400); // 2020-09-13T00:00Z
            let offset = 2 * 3600; // UTC+2
            let local_midnight_utc = day_start_utc + 86_400 - i64::from(offset); // 22:00Z
            let intervals = [(local_midnight_utc - 3_600, local_midnight_utc + 7_200)];
            let days = minutes_per_day(&intervals, offset);
            assert_eq!(
                days,
                vec![
                    ("2020-09-13".to_string(), 60),
                    ("2020-09-14".to_string(), 120),
                ]
            );
        }

        #[test]
        fn minutes_per_day_clamps_to_1440() {
            // Duplicate overlapping intervals summed past 24h clamp at 1440.
            let day_start = 1_600_000_000 - (1_600_000_000 % 86_400);
            let intervals = [
                (day_start, day_start + 86_400),
                (day_start, day_start + 86_400),
            ];
            let days = minutes_per_day(&intervals, 0);
            assert_eq!(days.len(), 1);
            assert_eq!(days[0].1, 1440);
        }

        #[test]
        fn local_date_respects_offset() {
            // 23:30 UTC is already the next day at UTC+1.
            let t = 1_600_000_000 - (1_600_000_000 % 86_400) - 1_800; // 2020-09-12T23:30Z
            assert_eq!(local_date(t, 0), "2020-09-12");
            assert_eq!(local_date(t, 3_600), "2020-09-13");
        }

        #[test]
        fn summarize_reports_today_and_total() {
            let days = vec![
                ("2026-06-03".to_string(), 312u32),
                ("2026-06-04".to_string(), 204u32),
            ];
            assert_eq!(
                summarize(&days, "2026-06-04"),
                "3.4h today, 8.6h over 7 days"
            );
            assert_eq!(summarize(&[], "2026-06-04"), "0.0h today, 0.0h over 7 days");
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    pub fn collect() -> Section {
        // The current local offset is applied to the whole window (best-effort: a
        // DST change mid-window shifts day boundaries by at most an hour).
        let now = chrono::Local::now();
        let now_unix = now.timestamp();
        let offset_secs = now.offset().local_minus_utc();

        let script = r#"
$err = $null
$out = @()
try {
  $ev = Get-WinEvent -FilterHashtable @{
    LogName = 'System'; ProviderName = 'Microsoft-Windows-Winlogon'
    Id = 7001,7002; StartTime = (Get-Date).AddDays(-7)
  } -ErrorAction Stop
  foreach ($e in $ev) {
    $t = [long]((Get-Date $e.TimeCreated).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    $out += [pscustomobject]@{ t = $t; id = [int]$e.Id }
  }
} catch {
  # "No events" in the window is not an error; anything else is reported.
  if ($_.Exception.Message -notmatch 'No events were found') { $err = [string]$_.Exception.Message }
}
[pscustomobject]@{ events = @($out); error = $err } | ConvertTo-Json -Compress -Depth 3
"#;

        let mut errors: Vec<String> = Vec::new();
        let events: Vec<(i64, core::Kind)> = match winps::run_json(script) {
            None => {
                errors.push("event log query failed".to_string());
                Vec::new()
            }
            Some(v) => {
                if let Some(err) = v.get("error").and_then(Value::as_str) {
                    errors.push(err.to_string());
                }
                v.get("events")
                    .cloned()
                    .map(winps::as_array)
                    .unwrap_or_default()
                    .iter()
                    .filter_map(|e| {
                        let t = e.get("t")?.as_i64()?;
                        let kind = match e.get("id")?.as_i64()? {
                            7001 => core::Kind::Logon,
                            7002 => core::Kind::Logoff,
                            _ => return None,
                        };
                        Some((t, kind))
                    })
                    .collect()
            }
        };

        let intervals = core::intervals_from_events(events, now_unix);
        let days = core::minutes_per_day(&intervals, offset_secs);
        let today = core::local_date(now_unix, offset_secs);
        let summary = core::summarize(&days, &today);
        // Only day-bucket aggregates go on the wire — never raw event timestamps.
        let day_values: Vec<Value> = days
            .iter()
            .map(|(date, minutes)| json!({ "date": date, "active_minutes": minutes }))
            .collect();

        Section::with_fields(
            Status::Ok,
            summary,
            json!({
                "window_days": WINDOW_DAYS,
                "days": day_values,
                "source": "eventlog",
                "errors": errors,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn screen_time_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert_eq!(v["window_days"], WINDOW_DAYS);
        assert!(v["days"].is_array());
        assert_eq!(v["source"], "eventlog");
        assert!(v["errors"].is_array());
        // Privacy: day buckets only — no usernames, no fine-grained timestamps.
        for day in v["days"].as_array().unwrap() {
            assert!(day["date"].is_string());
            assert!(day["active_minutes"].is_number());
            assert_eq!(day.as_object().unwrap().len(), 2);
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["days"].as_array().unwrap().len(), 0);
        assert_eq!(v["errors"].as_array().unwrap().len(), 0);
    }
}
