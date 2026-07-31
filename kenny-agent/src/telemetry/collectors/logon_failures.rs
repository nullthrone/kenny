//! `logon_failures` section — failed sign-in attempts per account, last 24 h.
//!
//! Source is Windows Security event **4625** ("An account failed to log on"),
//! grouped by target account name and by logon type. Frequent failures on a parent's
//! account mean something different from frequent failures over RDP, which is why
//! `types` is carried and source addresses are not: the distinction the operator
//! needs is *interactive vs remote*, not *which IP*.
//!
//! **Privacy:** this names accounts, which the rest of kenny's parental-controls
//! telemetry deliberately does not. That is the line ADR-0046 draws — an
//! authentication attempt belongs to the identity plane kenny governs, while
//! behaviour (`screen_time`, `web_activity`) stays whole-machine and unattributed.
//! Attempts against names that are not accounts on this machine collapse into
//! `unmatched_count`; a mistyped or probed username is not an identity.
//!
//! Reading the Security log needs LocalSystem, which the service has. An unreadable
//! log yields an empty section rather than a failed snapshot.

use crate::protocol::Status;
use crate::telemetry::Section;

/// Rolling window, in hours. Matches `web_activity`'s window so the two
/// parental-controls signals line up in the drill-down.
const WINDOW_HOURS: u32 = 24;

/// Cap on reported accounts. A machine under a password-spray attempt can produce
/// hundreds of distinct target names; the top offenders answer the question and the
/// rest are represented by `truncated`.
const MAX_ACCOUNTS: usize = 50;

/// Collect the `logon_failures` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", core::empty())
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use std::collections::BTreeMap;

    use serde_json::{json, Value};

    use super::{MAX_ACCOUNTS, WINDOW_HOURS};

    /// Windows logon types, reduced to the three that tell different stories.
    ///
    /// 2 = interactive at the console, 10 = RemoteInteractive (RDP), 11 =
    /// CachedInteractive (console, cached domain credentials). Everything else —
    /// network (3), batch (4), service (5), unlock (7) — is reported as `network`,
    /// because for this signal the only question is "was somebody at the keyboard".
    pub fn logon_type_token(logon_type: i64) -> &'static str {
        match logon_type {
            2 | 11 => "interactive",
            10 => "remote",
            _ => "network",
        }
    }

    /// The empty payload, used off-Windows and when the log cannot be read.
    pub fn empty() -> Value {
        json!({
            "window_hours": WINDOW_HOURS,
            "accounts": [],
            "unmatched_count": 0,
            "count": 0,
            "truncated": false,
        })
    }

    /// Aggregate raw `(target_name, logon_type)` events into the section payload.
    ///
    /// `known_accounts` decides which names are real accounts on this machine;
    /// everything else is counted into `unmatched_count` without being named. The
    /// match is case-insensitive because Windows treats account names that way and
    /// the event log does not normalize what the user typed.
    ///
    /// Accounts are sorted by descending count, then by name so the output is
    /// deterministic for equal counts (fixtures and diffs depend on that).
    pub fn shape(events: &[(String, i64)], known_accounts: &[String]) -> Value {
        let known: Vec<String> = known_accounts.iter().map(|n| n.to_lowercase()).collect();
        let mut per_account: BTreeMap<String, (u64, Vec<&'static str>)> = BTreeMap::new();
        let mut unmatched = 0u64;
        let mut total = 0u64;

        for (raw_name, logon_type) in events {
            let name = raw_name.trim();
            if name.is_empty() || name == "-" {
                continue;
            }
            total += 1;
            let Some(canonical) = known
                .iter()
                .position(|k| *k == name.to_lowercase())
                .map(|i| known_accounts[i].clone())
            else {
                unmatched += 1;
                continue;
            };
            let entry = per_account.entry(canonical).or_insert((0, Vec::new()));
            entry.0 += 1;
            let token = logon_type_token(*logon_type);
            if !entry.1.contains(&token) {
                entry.1.push(token);
            }
        }

        let mut rows: Vec<(String, u64, Vec<&'static str>)> = per_account
            .into_iter()
            .map(|(name, (count, mut types))| {
                types.sort_unstable();
                (name, count, types)
            })
            .collect();
        rows.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));

        let truncated = rows.len() > MAX_ACCOUNTS;
        rows.truncate(MAX_ACCOUNTS);

        json!({
            "window_hours": WINDOW_HOURS,
            "accounts": rows
                .into_iter()
                .map(|(name, count, types)| json!({
                    "name": name, "count": count, "types": types,
                }))
                .collect::<Vec<Value>>(),
            "unmatched_count": unmatched,
            "count": total,
            "truncated": truncated,
        })
    }

    /// Human summary for the section header.
    pub fn summary(payload: &Value) -> String {
        let count = payload["count"].as_u64().unwrap_or(0);
        let plural = if count == 1 { "" } else { "s" };
        format!("{count} failed logon{plural} in {WINDOW_HOURS}h")
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn known() -> Vec<String> {
            vec!["papa".to_string(), "kid".to_string()]
        }

        #[test]
        fn logon_types_collapse_to_three_tokens() {
            assert_eq!(logon_type_token(2), "interactive");
            assert_eq!(logon_type_token(11), "interactive");
            assert_eq!(logon_type_token(10), "remote");
            assert_eq!(logon_type_token(3), "network");
            assert_eq!(logon_type_token(5), "network");
        }

        #[test]
        fn shape_groups_by_account_and_dedups_types() {
            let events = vec![
                ("papa".to_string(), 2),
                ("papa".to_string(), 2),
                ("papa".to_string(), 10),
                ("kid".to_string(), 2),
            ];
            let out = shape(&events, &known());
            assert_eq!(out["count"], 4);
            assert_eq!(out["unmatched_count"], 0);
            // Descending by count, so papa leads.
            assert_eq!(out["accounts"][0]["name"], "papa");
            assert_eq!(out["accounts"][0]["count"], 3);
            assert_eq!(
                out["accounts"][0]["types"],
                json!(["interactive", "remote"])
            );
            assert_eq!(out["accounts"][1]["name"], "kid");
            assert_eq!(out["accounts"][1]["types"], json!(["interactive"]));
        }

        #[test]
        fn unknown_names_are_counted_but_never_named() {
            let events = vec![
                ("papa".to_string(), 2),
                ("administrateur".to_string(), 3),
                ("root".to_string(), 3),
            ];
            let out = shape(&events, &known());
            assert_eq!(out["count"], 3);
            assert_eq!(out["unmatched_count"], 2);
            assert_eq!(out["accounts"].as_array().unwrap().len(), 1);
            // A probed username must not become a name in the payload.
            let dumped = out.to_string();
            assert!(!dumped.contains("administrateur"));
            assert!(!dumped.contains("root"));
        }

        #[test]
        fn account_matching_is_case_insensitive_and_reports_the_real_name() {
            let out = shape(&[("PAPA".to_string(), 2)], &known());
            assert_eq!(out["unmatched_count"], 0);
            // The canonical SAM name is reported, not what was typed — it is the
            // governance key the account_* tools take.
            assert_eq!(out["accounts"][0]["name"], "papa");
        }

        #[test]
        fn empty_and_placeholder_targets_are_ignored() {
            // Event 4625 uses "-" for an unavailable target name.
            let out = shape(&[("-".to_string(), 3), ("".to_string(), 3)], &known());
            assert_eq!(out["count"], 0);
            assert_eq!(out["unmatched_count"], 0);
            assert_eq!(out["accounts"].as_array().unwrap().len(), 0);
        }

        #[test]
        fn account_list_is_capped_and_flags_truncation() {
            let names: Vec<String> = (0..MAX_ACCOUNTS + 10).map(|i| format!("u{i:03}")).collect();
            let events: Vec<(String, i64)> = names.iter().map(|n| (n.clone(), 2)).collect();
            let out = shape(&events, &names);
            assert_eq!(out["accounts"].as_array().unwrap().len(), MAX_ACCOUNTS);
            assert_eq!(out["truncated"], true);
            // The cap hides names, never the total.
            assert_eq!(out["count"], (MAX_ACCOUNTS + 10) as u64);

            let out = shape(&[("papa".to_string(), 2)], &known());
            assert_eq!(out["truncated"], false);
        }

        #[test]
        fn summary_reads_naturally() {
            assert_eq!(summary(&empty()), "0 failed logons in 24h");
            assert_eq!(
                summary(&shape(&[("papa".to_string(), 2)], &known())),
                "1 failed logon in 24h"
            );
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Read event 4625 over the window, plus the machine's account names so the
    /// aggregation can tell a real account from a probed username.
    ///
    /// `Get-WinEvent -FilterHashtable` filters server-side in the log service, which
    /// is what keeps this within `winps::PROBE_BUDGET` on a machine with a large
    /// Security log; `-MaxEvents` bounds the worst case regardless. `TargetUserName`
    /// and `LogonType` are read positionally from the event's data properties, whose
    /// order is fixed by the event schema and therefore locale-proof.
    pub fn collect() -> Section {
        let script = r#"
$since = (Get-Date).AddHours(-24)
$rows = @()
try {
  $rows = @(Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625; StartTime=$since} `
      -MaxEvents 2000 -ErrorAction Stop | ForEach-Object {
    $x = [xml]$_.ToXml()
    $d = @{}
    foreach ($p in $x.Event.EventData.Data) { $d[[string]$p.Name] = [string]$p.'#text' }
    [pscustomobject]@{
      name = [string]$d['TargetUserName']
      logon_type = [int]($d['LogonType'] -as [int])
    }
  })
} catch {}
$known = @()
try { $known = @(Get-LocalUser -ErrorAction Stop | ForEach-Object { [string]$_.Name }) } catch {}
ConvertTo-Json -Compress -Depth 4 ([pscustomobject]@{ events = @($rows); known = @($known) })
"#;

        let Some(probe) = winps::run_json(script) else {
            return Section::with_fields(Status::Ok, core::summary(&core::empty()), core::empty());
        };

        let events: Vec<(String, i64)> = probe
            .get("events")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default()
            .iter()
            .filter_map(|row| {
                Some((
                    row.get("name")?.as_str()?.to_string(),
                    row.get("logon_type").and_then(serde_json::Value::as_i64)?,
                ))
            })
            .collect();
        let known: Vec<String> = probe
            .get("known")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default()
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();

        let payload = core::shape(&events, &known);
        Section::with_fields(Status::Ok, core::summary(&payload), payload)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logon_failures_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["accounts"].is_array());
        assert!(v["count"].is_number());
        assert!(v["unmatched_count"].is_number());
        assert_eq!(v["window_hours"], 24);
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["accounts"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
    }
}
