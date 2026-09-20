//! `reliability` section — what is going wrong on the PC, not just how much.
//!
//! Reports a breakdown of the Error/Critical entries in the System + Application
//! event logs over a rolling window (7 days), grouped by source + event id, each
//! with a sample message and a per-day histogram, plus the boot instants inside
//! the window. Real data from `Get-WinEvent` on Windows; the server decides what
//! any of it means.

use serde_json::json;
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// How many days of event-log history the breakdown covers.
const WINDOW_DAYS: u64 = 7;
/// Cap the number of event groups reported so the frame stays bounded. At
/// ~400 bytes per group this is ~16 KB, comfortably inside the telemetry
/// frame cap.
#[cfg_attr(not(windows), allow(dead_code))]
const MAX_GROUPS: usize = 40;
/// Slots held for `level: "critical"` groups before any count-based selection.
/// A bugcheck fires once; a chatty-but-harmless provider fires thousands of
/// times. Selecting on count alone drops exactly the events the server scores.
#[cfg_attr(not(windows), allow(dead_code))]
const CRITICAL_SLOTS: usize = 8;
/// Slots held for the most recently seen groups, so a brand-new problem that
/// has not had time to accumulate a count still reaches the server.
#[cfg_attr(not(windows), allow(dead_code))]
const RECENT_SLOTS: usize = 8;
/// Cap on the reported boot instants (a host that reboot-loops must not be
/// able to grow the frame without bound).
#[cfg(windows)]
const MAX_BOOT_SESSIONS: usize = 20;

/// Pick which event groups to report, and say how many were dropped.
///
/// Three tiers, in order: every `level: "critical"` group (by count, up to
/// [`CRITICAL_SLOTS`]), then the most recently seen of what is left (up to
/// [`RECENT_SLOTS`]), then the largest by count until [`MAX_GROUPS`] is full.
/// The result is returned in count-descending order.
///
/// Deliberately not `#[cfg(windows)]`: the Windows collector cannot run on a
/// Linux CI box, so the selection policy — the part with a decision in it —
/// lives here where it can be tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
fn select_groups(groups: Vec<Value>) -> (Vec<Value>, usize) {
    if groups.len() <= MAX_GROUPS {
        return (sorted_by_count(groups), 0);
    }
    let dropped = groups.len() - MAX_GROUPS;

    // `last_seen` is a fixed-width RFC3339 UTC string, so lexicographic order
    // is chronological order and no date parsing is needed here.
    let keys: Vec<(usize, u64, bool, &str)> = groups
        .iter()
        .enumerate()
        .map(|(i, g)| {
            (
                i,
                g.get("count").and_then(Value::as_u64).unwrap_or(0),
                g.get("level").and_then(Value::as_str) == Some("critical"),
                g.get("last_seen").and_then(Value::as_str).unwrap_or(""),
            )
        })
        .collect();

    let mut taken = vec![false; groups.len()];
    let mut picked: Vec<usize> = Vec::with_capacity(MAX_GROUPS);

    let take =
        |ordered: Vec<usize>, limit: usize, taken: &mut Vec<bool>, picked: &mut Vec<usize>| {
            for idx in ordered.into_iter().take(limit) {
                if !taken[idx] && picked.len() < MAX_GROUPS {
                    taken[idx] = true;
                    picked.push(idx);
                }
            }
        };

    let mut critical: Vec<&(usize, u64, bool, &str)> = keys.iter().filter(|k| k.2).collect();
    critical.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    take(
        critical.iter().map(|k| k.0).collect(),
        CRITICAL_SLOTS,
        &mut taken,
        &mut picked,
    );

    let mut recent: Vec<&(usize, u64, bool, &str)> = keys.iter().filter(|k| !taken[k.0]).collect();
    recent.sort_by(|a, b| b.3.cmp(a.3).then(a.0.cmp(&b.0)));
    take(
        recent.iter().map(|k| k.0).collect(),
        RECENT_SLOTS,
        &mut taken,
        &mut picked,
    );

    let mut by_count: Vec<&(usize, u64, bool, &str)> =
        keys.iter().filter(|k| !taken[k.0]).collect();
    by_count.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    take(
        by_count.iter().map(|k| k.0).collect(),
        MAX_GROUPS,
        &mut taken,
        &mut picked,
    );

    let mut out: Vec<Value> = Vec::with_capacity(picked.len());
    for (i, g) in groups.into_iter().enumerate() {
        if taken[i] {
            out.push(g);
        }
    }
    (sorted_by_count(out), dropped)
}

#[cfg_attr(not(windows), allow(dead_code))]
fn sorted_by_count(mut groups: Vec<Value>) -> Vec<Value> {
    groups.sort_by_key(|g| std::cmp::Reverse(g.get("count").and_then(Value::as_u64).unwrap_or(0)));
    groups
}

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
                "boot_sessions": [],
                "truncated": false,
                "truncated_count": 0,
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
    /// last-seen, and a per-day histogram. Also read the boot instants in the
    /// window and the latest reliability stability index. The heavy lifting is in
    /// PowerShell; Rust shapes the result.
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
  # The newest member of the group: a generic event id groups unrelated
  # failures together, so `sample` names whichever one happened last rather
  # than summarising the group. docs/protocol.md says so on the wire.
  $latest = $g.Group | Sort-Object TimeCreated -Descending | Select-Object -First 1
  $level = if ($latest.Level -eq 1) { 'critical' } else { 'error' }
  $msg = if ($latest.Message) { ($latest.Message -split "`r?`n")[0] } else { '' }
  if ($msg.Length -gt 200) { $msg = $msg.Substring(0,200) }
  # UTC, to match `last_seen`. A local-date key makes the number of distinct
  # days -- which the server scores on -- depend on the host's timezone.
  $byDay = @{}
  foreach ($e in $g.Group) {
    $d = $e.TimeCreated.ToUniversalTime().ToString('yyyy-MM-dd')
    if ($byDay.ContainsKey($d)) { $byDay[$d]++ } else { $byDay[$d] = 1 }
  }
  $groups += [pscustomobject]@{
    source    = $latest.ProviderName
    event_id  = [int]$latest.Id
    level     = $level
    count     = [int]$g.Count
    sample    = $msg
    last_seen = $latest.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    by_day    = $byDay
  }
}
# Boot markers from the same log and the same clock as the events above, so
# "did this fire on the way down or across several boots?" is answerable
# without comparing against a tick-counter-derived uptime.
$boots = @()
foreach ($f in @(
  @{ LogName='System'; ProviderName='Microsoft-Windows-Kernel-Boot'; Id=20; StartTime=$since },
  @{ LogName='System'; ProviderName='EventLog'; Id=6005; StartTime=$since }
)) {
  try {
    $boots += @(Get-WinEvent -FilterHashtable $f -ErrorAction Stop |
                ForEach-Object { $_.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') })
  } catch {}
}
$boots = @($boots | Sort-Object -Unique)
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
  boot_sessions   = @($boots)
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

        let raw: Vec<Value> = v
            .get("groups")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let (groups, truncated_count) = select_groups(raw);

        let mut boot_sessions: Vec<Value> = v
            .get("boot_sessions")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if boot_sessions.len() > MAX_BOOT_SESSIONS {
            let from = boot_sessions.len() - MAX_BOOT_SESSIONS;
            boot_sessions.drain(..from);
        }

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
                "boot_sessions": boot_sessions,
                "truncated": truncated_count > 0,
                "truncated_count": truncated_count,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn group(source: &str, count: u64, level: &str, last_seen: &str) -> Value {
        json!({
            "source": source,
            "event_id": 1,
            "level": level,
            "count": count,
            "sample": "",
            "last_seen": last_seen,
            "by_day": {},
        })
    }

    fn sources(groups: &[Value]) -> Vec<&str> {
        groups
            .iter()
            .map(|g| g.get("source").and_then(Value::as_str).unwrap_or(""))
            .collect()
    }

    #[test]
    fn reliability_section_is_valid() {
        let v = collect().into_value();
        assert!(v.get("recent_crashes").is_some());
        // The breakdown is always present (empty on non-Windows).
        assert!(v.get("events").and_then(|e| e.as_array()).is_some());
        assert!(v.get("boot_sessions").and_then(|b| b.as_array()).is_some());
        assert!(v.get("window_days").is_some());
        assert_eq!(v.get("truncated_count").and_then(Value::as_u64), Some(0));
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

    #[test]
    fn short_group_list_is_kept_whole_and_sorted_by_count() {
        let raw = vec![
            group("small", 1, "error", "2026-06-01T00:00:00Z"),
            group("big", 99, "error", "2026-06-01T00:00:00Z"),
        ];
        let (out, dropped) = select_groups(raw);
        assert_eq!(dropped, 0);
        assert_eq!(sources(&out), vec!["big", "small"]);
    }

    #[test]
    fn a_lone_critical_group_survives_a_flood_of_noisy_ones() {
        // The ADR-0041 shape: one chatty provider dwarfs everything, and the
        // single bugcheck is the one event the server actually scores. Count
        // ordering alone would drop it.
        let mut raw: Vec<Value> = (0..MAX_GROUPS + 20)
            .map(|i| {
                group(
                    &format!("noise{i}"),
                    5000 + i as u64,
                    "error",
                    "2026-06-01T00:00:00Z",
                )
            })
            .collect();
        raw.push(group("Kernel-Power", 1, "critical", "2026-06-04T18:00:00Z"));

        let (out, dropped) = select_groups(raw);
        assert_eq!(out.len(), MAX_GROUPS);
        assert_eq!(dropped, 20 + 1);
        assert!(sources(&out).contains(&"Kernel-Power"));
    }

    #[test]
    fn a_new_low_count_group_survives_on_recency() {
        // A problem that started an hour ago has no count yet. Without the
        // reserved recency slots it would never reach the server.
        let mut raw: Vec<Value> = (0..MAX_GROUPS + 10)
            .map(|i| {
                group(
                    &format!("noise{i}"),
                    900 + i as u64,
                    "error",
                    "2026-06-01T00:00:00Z",
                )
            })
            .collect();
        raw.push(group("BrandNew", 2, "error", "2026-06-04T17:59:00Z"));

        let (out, _) = select_groups(raw);
        assert!(sources(&out).contains(&"BrandNew"));
    }

    #[test]
    fn no_tier_can_consume_the_whole_budget() {
        // A reboot loop emits many critical groups, and they are also the most
        // recent thing on the box -- so they draw on both reserved tiers. That
        // is intended (a critical event minutes old is worth keeping twice
        // over), but the count tier must still get the rest of the budget, or
        // one bad night would hide everything else the host is doing.
        let mut raw: Vec<Value> = (0..20)
            .map(|i| group(&format!("crit{i}"), 1, "critical", "2026-06-04T18:00:00Z"))
            .collect();
        raw.extend((0..MAX_GROUPS).map(|i| {
            group(
                &format!("loud{i}"),
                1000 + i as u64,
                "error",
                "2026-06-01T00:00:00Z",
            )
        }));

        let (out, _) = select_groups(raw);
        assert_eq!(out.len(), MAX_GROUPS);
        let kept = sources(&out);
        let crit_kept = kept.iter().filter(|s| s.starts_with("crit")).count();
        let loud_kept = kept.iter().filter(|s| s.starts_with("loud")).count();
        assert!(
            crit_kept <= CRITICAL_SLOTS + RECENT_SLOTS,
            "kept {crit_kept} critical groups"
        );
        assert_eq!(loud_kept, MAX_GROUPS - CRITICAL_SLOTS - RECENT_SLOTS);
    }
}
