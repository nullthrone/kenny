//! `logon_failures` section — failed sign-in attempts per account, last 24 h.
//!
//! Source is Windows Security event **4625** ("An account failed to log on"),
//! grouped by target account name and by logon type. Frequent failures on a parent's
//! account mean something different from frequent failures over RDP, which is why
//! `types` is carried and source addresses are not: the distinction the operator
//! needs is *interactive vs remote*, not *which IP*.
//!
//! **Privacy:** this names accounts, which the rest of kenny's parental-controls
//! telemetry deliberately does not. That is the line ADR-0042 draws — an
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
    #[cfg(target_os = "linux")]
    {
        linux_impl::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
        let tokens: Vec<(String, &'static str)> = events
            .iter()
            .map(|(name, logon_type)| (name.clone(), logon_type_token(*logon_type)))
            .collect();
        shape_tokens(&tokens, known_accounts)
    }

    /// As [`shape`], but taking events already classified into the wire vocabulary.
    ///
    /// The Linux arm has no logon-type numbers to translate — it reads a service tag
    /// (`sshd`, `sudo`, `gdm-password`) and classifies directly. Routing it through
    /// synthetic Windows type codes would bake Windows numerology into a Linux
    /// parser; both arms instead meet here, on one aggregation path (ADR-0043).
    pub fn shape_tokens(events: &[(String, &'static str)], known_accounts: &[String]) -> Value {
        let known: Vec<String> = known_accounts.iter().map(|n| n.to_lowercase()).collect();
        let mut per_account: BTreeMap<String, (u64, Vec<&'static str>)> = BTreeMap::new();
        let mut unmatched = 0u64;
        let mut total = 0u64;

        for (raw_name, token) in events {
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
            if !entry.1.contains(token) {
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

    /// Classify a syslog service tag into the wire vocabulary.
    ///
    /// `sshd` is the Linux remote sign-in plane, so it maps to `remote` — the same
    /// equivalence `deny_logon`'s `remote_interactive` makes, which keeps one story
    /// across both features. Everything else kenny recognizes happens at the machine:
    /// a console login, a display manager, or a failed `sudo`/`su` elevation. Windows
    /// logs a 4625 with logon type 2 for a failed UAC elevation too, so counting
    /// `sudo` here is closer to the Windows behaviour, not further from it.
    ///
    /// `network` is deliberately unreachable on Linux: there is no per-account
    /// network-logon plane, and an absent token means "no failures of that kind",
    /// which is true, rather than a coverage claim (ADR-0043).
    ///
    /// Returns `None` for a tag kenny does not recognize, so unrelated auth-facility
    /// chatter (cron, dbus, systemd) is dropped rather than counted as a sign-in.
    // Linux-only parsing, kept in `core` so it is unit-tested everywhere; off
    // Linux the tests are its only consumers. `logon_type_token` above is the
    // Windows-only mirror, covered by this module's own `not(windows)` allow.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn service_token(tag: &str) -> Option<&'static str> {
        // OpenSSH 9.8+ splits the listener into `sshd-session`.
        if tag.starts_with("sshd") {
            return Some("remote");
        }
        match tag {
            "sudo" | "su" | "login" | "gdm-password" | "gdm" | "sddm" | "lightdm" | "polkit"
            | "polkitd" | "xdm" | "systemd-logind" => Some("interactive"),
            _ => None,
        }
    }

    /// Split a syslog line into its service tag and message.
    ///
    /// Lines look like `Jul 31 12:00:00 host sshd[1234]: Failed password for …`.
    /// The tag is the last token before the first `: `, with any `[pid]` stripped.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    fn split_tag(line: &str) -> Option<(&str, &str)> {
        let (prefix, msg) = line.split_once(": ")?;
        let tag = prefix.split_whitespace().next_back()?;
        let tag = tag.split('[').next()?;
        (!tag.is_empty()).then_some((tag, msg))
    }

    /// Pull the target account out of an authentication-failure message.
    ///
    /// Ordered by specificity. `user=` (the PAM form) comes first because it is the
    /// one field that is unambiguous; the sshd prose forms follow.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    fn extract_user(msg: &str) -> Option<&str> {
        let after = |marker: &str| -> Option<&str> {
            let rest = msg.split_once(marker)?.1.trim_start();
            let end = rest
                .find(|c: char| c.is_whitespace() || c == ',' || c == '\'')
                .unwrap_or(rest.len());
            Some(&rest[..end]).filter(|u| !u.is_empty())
        };
        // pam_unix(sshd:auth): authentication failure; … rhost=… user=papa
        if let Some(user) = after(" user=") {
            return Some(user);
        }
        // Failed password for invalid user bob from …  /  Failed publickey for bob …
        for marker in [" for invalid user ", " for illegal user ", " for "] {
            if msg.starts_with("Failed ") || msg.starts_with("error: maximum authentication") {
                if let Some(user) = after(marker) {
                    return Some(user);
                }
            }
        }
        // Invalid user bob from 1.2.3.4
        if msg.starts_with("Invalid user ") {
            return after("Invalid user ");
        }
        // FAILED LOGIN (1) on '/dev/tty1' FOR 'papa', Authentication failure
        if msg.contains("FAILED LOGIN") {
            return after(" FOR '");
        }
        None
    }

    /// Does this message describe a *failed authentication*?
    ///
    /// The auth facility carries plenty of successful and unrelated events; matching
    /// the failure phrasings explicitly keeps a successful `sudo` out of the count.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    fn is_failure(msg: &str) -> bool {
        msg.contains("authentication failure")
            || msg.contains("Failed password")
            || msg.contains("Failed publickey")
            || msg.contains("Failed keyboard-interactive")
            || msg.contains("Invalid user")
            || msg.contains("FAILED LOGIN")
            || msg.contains("incorrect password attempt")
    }

    /// Parse syslog/journal lines into classified failure events.
    ///
    /// Lives in the portable core so the patterns are unit-tested on every platform,
    /// including Windows CI, exactly as `parse_system_access` is.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn parse_auth_lines<'a>(
        lines: impl Iterator<Item = &'a str>,
    ) -> Vec<(String, &'static str)> {
        let mut out = Vec::new();
        for line in lines {
            let Some((tag, msg)) = split_tag(line) else {
                continue;
            };
            let Some(token) = service_token(tag) else {
                continue;
            };
            if !is_failure(msg) {
                continue;
            }
            if let Some(user) = extract_user(msg) {
                out.push((user.to_string(), token));
            }
        }
        out
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
        fn auth_lines_classify_ssh_as_remote_and_the_console_as_interactive() {
            let log = "\
Jul 31 10:00:01 nas sshd[1201]: Failed password for papa from 10.0.0.5 port 51234 ssh2
Jul 31 10:00:04 nas sshd[1201]: Failed password for invalid user admin from 10.0.0.5 port 51235 ssh2
Jul 31 10:00:09 nas sshd-session[1300]: Failed publickey for kid from 10.0.0.9 port 51240 ssh2
Jul 31 10:01:00 nas sshd[1400]: Invalid user oracle from 203.0.113.7 port 40000
Jul 31 10:02:00 nas sudo[2000]: pam_unix(sudo:auth): authentication failure; logname=kid uid=1001 euid=0 tty=/dev/pts/0 ruser=kid rhost=  user=papa
Jul 31 10:03:00 nas login[900]: FAILED LOGIN (1) on '/dev/tty1' FOR 'kid', Authentication failure
Jul 31 10:04:00 nas sshd[1500]: Accepted password for papa from 10.0.0.5 port 51299 ssh2
Jul 31 10:05:00 nas CRON[3000]: pam_unix(cron:session): session opened for user root
";
            let events = parse_auth_lines(log.lines());
            assert_eq!(
                events,
                vec![
                    ("papa".to_string(), "remote"),
                    ("admin".to_string(), "remote"),
                    ("kid".to_string(), "remote"),
                    ("oracle".to_string(), "remote"),
                    // A failed sudo is an elevation attempt at the machine, which is
                    // what Windows logs as logon type 2 as well.
                    ("papa".to_string(), "interactive"),
                    ("kid".to_string(), "interactive"),
                ],
                "a successful sign-in and unrelated cron chatter must not be counted"
            );
        }

        #[test]
        fn linux_never_reports_the_network_type() {
            // `network` has no Linux meaning; an absent token says "no failures of
            // that kind", which is true, rather than claiming coverage (ADR-0043).
            assert_eq!(service_token("sshd"), Some("remote"));
            assert_eq!(service_token("sshd-session"), Some("remote"));
            assert_eq!(service_token("gdm-password"), Some("interactive"));
            assert_eq!(service_token("cron"), None);
            assert_eq!(service_token("systemd"), None);
        }

        #[test]
        fn shape_tokens_is_the_same_aggregation_as_shape() {
            let events = vec![
                ("papa".to_string(), "interactive"),
                ("papa".to_string(), "interactive"),
                ("papa".to_string(), "remote"),
                ("kid".to_string(), "interactive"),
            ];
            let by_token = shape_tokens(&events, &known());
            let by_type = shape(
                &[
                    ("papa".to_string(), 2),
                    ("papa".to_string(), 2),
                    ("papa".to_string(), 10),
                    ("kid".to_string(), 2),
                ],
                &known(),
            );
            assert_eq!(by_token, by_type);
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

#[cfg(target_os = "linux")]
mod linux_impl {
    use std::process::Command;

    use super::*;
    use crate::telemetry::collectors::local_accounts::linux_impl as accounts;

    /// Cap on how much of a plain-text auth log is read, in bytes.
    ///
    /// The journal query is already time-bounded; a file fallback is not, and a
    /// long-lived server's `auth.log` can be very large. The tail is what the last
    /// 24 h are in, and a bounded read cannot turn a snapshot into an OOM.
    const MAX_LOG_BYTES: u64 = 2 * 1024 * 1024;

    /// Auth events from the journal, time-bounded to the reporting window.
    ///
    /// `-o short` keeps the `tag[pid]:` prefix that carries the service name — the
    /// thing the classification depends on — which `-o cat` would strip.
    fn journal() -> Option<String> {
        let out = Command::new("journalctl")
            .args([
                "--since",
                "-24h",
                "--facility=auth,authpriv",
                "--no-pager",
                "-q",
                "-o",
                "short",
            ])
            .output()
            .ok()?;
        out.status
            .success()
            .then(|| String::from_utf8_lossy(&out.stdout).into_owned())
            .filter(|text| !text.trim().is_empty())
    }

    /// Debian's `auth.log` / RHEL's `secure`, read from the tail.
    ///
    /// Syslog timestamps carry no year, so the 24 h window cannot be enforced from
    /// the file itself; the byte cap is the approximation. Stated rather than
    /// hidden — this is the fallback for hosts without a journal.
    fn log_file() -> Option<String> {
        use std::io::{Read, Seek, SeekFrom};
        for path in ["/var/log/auth.log", "/var/log/secure"] {
            let Ok(mut file) = std::fs::File::open(path) else {
                continue;
            };
            let len = file.metadata().ok()?.len();
            if len > MAX_LOG_BYTES {
                file.seek(SeekFrom::End(-(MAX_LOG_BYTES as i64))).ok()?;
            }
            let mut buf = Vec::new();
            file.read_to_end(&mut buf).ok()?;
            return Some(String::from_utf8_lossy(&buf).into_owned());
        }
        None
    }

    pub fn collect() -> Section {
        // Neither source available (no journal, no readable log): report the same
        // empty payload as an unreadable Windows Security log rather than faulting.
        let Some(text) = journal().or_else(log_file) else {
            return Section::with_fields(Status::Ok, "n/a on this platform", core::empty());
        };
        let events = core::parse_auth_lines(text.lines());
        // Matched against the same account set the `local_accounts` panel lists, so
        // an attempt against a service account collapses into `unmatched_count`
        // instead of naming a row the operator cannot see.
        let known = std::fs::read_to_string("/etc/passwd")
            .map(|passwd| accounts::login_account_names(&passwd))
            .unwrap_or_default();

        let payload = core::shape_tokens(&events, &known);
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

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["accounts"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
    }
}
