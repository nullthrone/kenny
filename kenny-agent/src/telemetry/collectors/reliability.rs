//! `reliability` section — what is going wrong on the PC, not just how much.
//!
//! Reports a breakdown of the Error/Critical entries in the System + Application
//! event logs over a rolling window (7 days), grouped by source + event id, each
//! with a sample message and a per-day histogram. Real data from `Get-WinEvent`
//! on Windows; the server categorizes the groups and draws the heatmaps.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// How many days of event-log history the breakdown covers.
const WINDOW_DAYS: u64 = 7;
/// Cap the number of event groups reported so the frame stays bounded.
#[cfg(windows)]
const MAX_GROUPS: usize = 20;

/// Collect the `reliability` section.
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
                "stability_index": null,
                "recent_crashes": 0,
                "window_days": WINDOW_DAYS,
                "events": [],
                "truncated": false,
            }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Group Error/Critical (Level 1/2) events in the System + Application logs
    /// over the last 7 days by (ProviderName, Id): count, level, a sample message,
    /// last-seen, and a per-day histogram. Also read the latest reliability
    /// stability index. The heavy lifting is in PowerShell; Rust shapes the result.
    pub fn collect() -> Section {
        let script = r#"
$since = (Get-Date).AddDays(-7)
$events = @()
foreach ($log in 'System','Application') {
  try {
    $events += @(Get-WinEvent -FilterHashtable @{ LogName=$log; Level=1,2; StartTime=$since } -ErrorAction Stop)
  } catch {}
}
$groups = @()
foreach ($g in ($events | Group-Object ProviderName, Id)) {
  $first = $g.Group | Sort-Object TimeCreated -Descending | Select-Object -First 1
  $level = if ($first.Level -eq 1) { 'critical' } else { 'error' }
  $msg = if ($first.Message) { ($first.Message -split "`r?`n")[0] } else { '' }
  if ($msg.Length -gt 200) { $msg = $msg.Substring(0,200) }
  $byDay = @{}
  foreach ($e in $g.Group) {
    $d = $e.TimeCreated.ToString('yyyy-MM-dd')
    if ($byDay.ContainsKey($d)) { $byDay[$d]++ } else { $byDay[$d] = 1 }
  }
  $groups += [pscustomobject]@{
    source    = $first.ProviderName
    event_id  = [int]$first.Id
    level     = $level
    count     = [int]$g.Count
    sample    = $msg
    last_seen = $first.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    by_day    = $byDay
  }
}
$total = ($events | Measure-Object).Count
$index = $null
try {
  $m = Get-CimInstance -ClassName Win32_ReliabilityStabilityMetrics -ErrorAction Stop |
       Sort-Object TimeGenerated -Descending | Select-Object -First 1
  if ($m) { $index = [double]$m.SystemStabilityIndex }
} catch {}
[pscustomobject]@{
  stability_index = $index
  recent_crashes  = $total
  groups          = @($groups)
} | ConvertTo-Json -Depth 6 -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            // A probe that timed out or failed carries NO reading, and must not
            // be mistaken for one: reporting `recent_crashes: 0` here claims the
            // host had zero error events, which reads as a clean bill of health
            // and clears any standing alarm until the next push says otherwise.
            // Reporting only `status` + `summary` -- all the contract requires
            // of a section -- makes the server's reliability rule defer instead
            // (`health_rules._rule_reliability` returns None when the payload
            // carries no events, no total and no index), so the section shows
            // "unavailable" rather than "fine".
            return Section::with_fields(Status::Warn, "reliability unavailable", json!({}));
        };

        let total = v.get("recent_crashes").and_then(Value::as_u64).unwrap_or(0);
        let index = v.get("stability_index").cloned().unwrap_or(Value::Null);

        // Sort groups by count desc and cap to MAX_GROUPS so the frame stays bounded.
        let mut groups: Vec<Value> = v
            .get("groups")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        groups.sort_by_key(|g| {
            std::cmp::Reverse(g.get("count").and_then(Value::as_u64).unwrap_or(0))
        });
        let truncated = groups.len() > MAX_GROUPS;
        groups.truncate(MAX_GROUPS);

        // Report what happened; do not grade it. Every threshold that decides
        // whether these counts are worth an operator's attention lives in the
        // server's `health_rules.py`, and the server does not fold this status
        // into the rule's verdict (see docs/protocol.md, this section). A
        // grade here would be one the server cannot lower and one this binary
        // cannot change without being redeployed: the old
        // `total >= 20 -> Warn` bar is cleared by every real Windows PC, which
        // pinned the section at `warn` no matter what the server decided.
        let summary = format!("{total} error/critical events in 7d");

        Section::with_fields(
            Status::Ok,
            summary,
            json!({
                "stability_index": index,
                "recent_crashes": total,
                "window_days": WINDOW_DAYS,
                "events": groups,
                "truncated": truncated,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reliability_section_is_valid() {
        let v = collect().into_value();
        assert!(v.get("recent_crashes").is_some());
        // The breakdown is always present (empty on non-Windows).
        assert!(v.get("events").and_then(|e| e.as_array()).is_some());
        assert!(v.get("window_days").is_some());
    }

    #[test]
    fn reliability_never_grades_the_host() {
        // The server's `health_rules.py` owns every reliability threshold and
        // does not fold this status into its verdict (docs/protocol.md). A
        // grade here is one the server cannot lower -- see the comment on the
        // summary in `windows_impl::collect`. The Windows path is not
        // reachable in this test on a non-Windows runner; the assertion holds
        // for the portable stub and pins the intent for both.
        assert_eq!(collect().into_value().get("status").unwrap(), "ok");
    }
}
