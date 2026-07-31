//! `local_accounts` section — local users plus Administrators-group membership.
//!
//! Real data from `Get-LocalUser` on Windows; admin membership comes from
//! `Get-LocalGroupMember -SID S-1-5-32-544` (the well-known Administrators group
//! SID, locale-proof) and is matched to users by SID. Built-ins are marked via the
//! well-known RID suffixes (`-500` Administrator, `-501` Guest).
//!
//! Since v0.15 this section is also the **inventory for the `account_*` governance
//! tools** (ADR-0046): each account carries its `kind` (a Microsoft account on a
//! workgroup PC is a SAM entry like any other), a `display` label, the LSA logon
//! rights currently denied, and the governance verbs it does *not* support.
//!
//! **Privacy/minimality:** full SIDs never go on the wire — all SID matching
//! happens inside the probe, and only booleans leave it (ADR-0026 stance,
//! docs/protocol.md v0.10). Since v0.15, **Microsoft-account email addresses never
//! go on the wire either** (ADR-0046): `display` falls back to the SAM name rather
//! than the `MicrosoftAccount\…` qualified form. Both rules are asserted by tests.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `local_accounts` section.
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
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({
                "accounts": [], "admins": [], "count": 0,
                "password_policy": core::password_policy(None, None, None),
            }),
        )
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    /// Account kind, from PowerShell's `PrincipalSource` (ADR-0046).
    ///
    /// This is the *only* axis on which governance verbs differ. It is deliberately
    /// not a routing switch: every `account_*` tool takes the same SAM name for
    /// every kind, because below the SAM/LSA layer Windows draws no distinction.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Kind {
        Local,
        Microsoft,
        Entra,
        Unknown,
    }

    impl Kind {
        /// Map a `PrincipalSource` string. Windows reports `MicrosoftAccount` and
        /// `AzureAD`; anything unrecognized (including a probe that could not read
        /// the property at all) is `Unknown` rather than being guessed as `Local` —
        /// a wrong `local` would advertise `reset_password` as supported.
        pub fn from_principal_source(source: Option<&str>) -> Kind {
            match source.map(str::trim) {
                Some("Local") => Kind::Local,
                Some("MicrosoftAccount") => Kind::Microsoft,
                Some("AzureAD") => Kind::Entra,
                _ => Kind::Unknown,
            }
        }

        pub fn as_str(self) -> &'static str {
            match self {
                Kind::Local => "local",
                Kind::Microsoft => "microsoft",
                Kind::Entra => "entra",
                Kind::Unknown => "unknown",
            }
        }

        /// Governance verbs this kind cannot perform, as `verb → reason token`.
        ///
        /// Negation, not enumeration: an absent verb is supported. That keeps the
        /// payload small and makes the wire format state the design — seamless is
        /// the default, asymmetry is the named exception (ADR-0046).
        pub fn unsupported(self) -> Vec<(&'static str, &'static str)> {
            match self {
                // A password living in Microsoft's / Entra's cloud identity cannot
                // be reset from the endpoint. No `create_local` entry here: creating
                // an account is not an operation *on* an existing account.
                Kind::Microsoft | Kind::Entra => vec![("reset_password", "password_in_cloud")],
                // Unknown is treated as the restrictive case for the same reason
                // `from_principal_source` refuses to guess.
                Kind::Unknown => vec![("reset_password", "kind_unknown")],
                Kind::Local => Vec::new(),
            }
        }
    }

    /// Logon rights kenny can deny, in stable wire order.
    ///
    /// LSA account rights on Windows; on Linux `remote_interactive` is an sshd
    /// `DenyUsers` entry and `network` has no counterpart (ADR-0047).
    ///
    /// `SeDenyInteractiveLogonRight` is deliberately absent: it can lock out the
    /// sole console user and kenny has no remote console to recover with (ADR-0046).
    pub const DENY_RIGHTS: [&str; 2] = ["network", "remote_interactive"];

    /// Governance verbs *this host* cannot perform, as `verb → reason token`.
    ///
    /// The third layer of the negation map, beside the account's own restrictions and
    /// its `Kind`'s (ADR-0047). Windows passes [`HostCaps::none`] and its payload is
    /// unchanged; Linux probes for `sshd`, `systemd-logind`, a graphical session, an
    /// admin group and a readable `/etc/shadow`, and reports what is missing.
    ///
    /// Host-level facts surface *per account* in the same `unsupported` map as the
    /// account-level ones: a consumer should never have to ask *why* a verb is
    /// unavailable in order to know *that* it is.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct HostCaps {
        gaps: Vec<(&'static str, &'static str)>,
    }

    impl HostCaps {
        /// No host-level gaps — the Windows case.
        pub fn none() -> HostCaps {
            HostCaps { gaps: Vec::new() }
        }

        /// Record that this host cannot perform `verb`, because `reason`.
        #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
        pub fn gap(&mut self, verb: &'static str, reason: &'static str) {
            self.gaps.push((verb, reason));
        }

        pub fn negations(&self) -> &[(&'static str, &'static str)] {
            &self.gaps
        }
    }

    /// One local account, as read from the probe (SID already reduced to booleans).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Account {
        pub name: String,
        /// Human label: `FullName` when set, else `name`. Never the
        /// `MicrosoftAccount\<address>` qualified form — see the module docs.
        pub display: String,
        pub kind: Kind,
        /// Denied LSA logon rights, a subset of [`DENY_RIGHTS`].
        pub deny_logon: Vec<String>,
        pub enabled: bool,
        pub is_admin: bool,
        pub password_required: bool,
        /// RFC3339 UTC of the last password change; `None` means a password was
        /// never set (genuinely password-less). PowerShell `PasswordRequired`
        /// reflects UF_PASSWD_NOTREQD ("blank password permitted"), NOT "has no
        /// password" — this field disambiguates. See ADR-0031.
        pub password_last_set: Option<String>,
        /// RFC3339 UTC, if the account ever logged on.
        pub last_logon: Option<String>,
        pub builtin_admin: bool,
        pub builtin_guest: bool,
        /// Governance verbs *this particular account* cannot perform, beyond whatever
        /// its [`Kind`] and its host already rule out — e.g. root, or an account whose
        /// admin rights come from `/etc/sudoers.d` rather than a group (ADR-0047).
        /// Most specific layer of the three, so it wins on conflict.
        pub extra_unsupported: Vec<(&'static str, &'static str)>,
    }

    impl Account {
        /// Build from one probe row; rows without a name are dropped, booleans
        /// default to `false` when the probe could not read them.
        pub fn from_row(row: &Value) -> Option<Account> {
            let name = row.get("name")?.as_str()?.to_string();
            let kind =
                Kind::from_principal_source(row.get("principal_source").and_then(Value::as_str));
            Some(Account {
                display: display_label(str_field(row, "display").as_deref(), &name),
                kind,
                deny_logon: DENY_RIGHTS
                    .iter()
                    .filter(|right| {
                        row.get("deny_logon")
                            .and_then(Value::as_array)
                            .is_some_and(|set| set.iter().any(|v| v.as_str() == Some(**right)))
                    })
                    .map(|right| (*right).to_string())
                    .collect(),
                name,
                enabled: bool_field(row, "enabled"),
                is_admin: bool_field(row, "is_admin"),
                password_required: bool_field(row, "password_required"),
                password_last_set: row
                    .get("password_last_set")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                last_logon: row
                    .get("last_logon")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                builtin_admin: bool_field(row, "builtin_admin"),
                builtin_guest: bool_field(row, "builtin_guest"),
                extra_unsupported: Vec::new(),
            })
        }
    }

    fn bool_field(row: &Value, key: &str) -> bool {
        row.get(key).and_then(Value::as_bool).unwrap_or(false)
    }

    /// Pick the human label for an account, falling back to the SAM name.
    ///
    /// A candidate containing `@` or `\` is rejected outright: those are the shapes
    /// of a Microsoft-account address and of the `MicrosoftAccount\<address>`
    /// qualified name, and neither may reach the wire (ADR-0046). The probe already
    /// avoids emitting them; this is the belt-and-braces check that a test can
    /// assert against, since the cost of being wrong is leaking an address.
    pub fn display_label(candidate: Option<&str>, name: &str) -> String {
        candidate
            .map(str::trim)
            .filter(|d| !d.is_empty() && !d.contains('@') && !d.contains('\\'))
            .unwrap_or(name)
            .to_string()
    }

    fn str_field(row: &Value, key: &str) -> Option<String> {
        row.get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .map(str::to_string)
    }

    /// Machine-wide password policy, carrying its own reach.
    ///
    /// `applies_to` is part of the payload rather than documentation because a
    /// policy that silently misses every Microsoft account is worse than none: a
    /// consumer that renders this must be able to say so without knowing ADR-0046.
    /// Any field is `null` when the probe could not read it.
    pub fn password_policy(
        min_length: Option<u32>,
        max_age_days: Option<u32>,
        lockout_threshold: Option<u32>,
    ) -> Value {
        password_policy_with_gaps(min_length, max_age_days, lockout_threshold, &[])
    }

    /// As [`password_policy`], plus the fields this host cannot set **at all**.
    ///
    /// The same negation idiom as the per-account map, one level up: a `null` value
    /// means "not read / not configured", an entry here means "this machine has no
    /// knob for it" (e.g. `pam_faillock` is not in the PAM stack). The key is omitted
    /// entirely when there are no gaps, so the Windows payload is unchanged.
    pub fn password_policy_with_gaps(
        min_length: Option<u32>,
        max_age_days: Option<u32>,
        lockout_threshold: Option<u32>,
        gaps: &[(&'static str, &'static str)],
    ) -> Value {
        let mut out = json!({
            "applies_to": "local_only",
            "min_length": min_length,
            "max_age_days": max_age_days,
            "lockout_threshold": lockout_threshold,
        });
        if !gaps.is_empty() {
            let map: serde_json::Map<String, Value> = gaps
                .iter()
                .map(|(field, reason)| ((*field).to_string(), Value::from(*reason)))
                .collect();
            out["unsupported"] = Value::Object(map);
        }
        out
    }

    /// Parse the `[System Access]` block of a `secedit /export` INF into the
    /// password-policy payload.
    ///
    /// Those key names are locale-invariant (unlike `net accounts` output, whose
    /// labels are translated), which is the whole reason this reads the INF. A key
    /// that is absent or unparsable stays `null` rather than defaulting to 0 — "no
    /// minimum length" and "we could not read the minimum length" must not look
    /// alike to a health rule. `MaximumPasswordAge = -1` means "never expires" and
    /// is normalized to 0, matching how Windows itself presents it.
    pub fn parse_system_access<'a>(lines: impl Iterator<Item = &'a str>) -> Value {
        let (mut min_length, mut max_age_days, mut lockout_threshold) = (None, None, None);
        for line in lines {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            let value = value.trim();
            let slot = match key.trim() {
                "MinimumPasswordLength" => &mut min_length,
                "MaximumPasswordAge" => &mut max_age_days,
                "LockoutBadCount" => &mut lockout_threshold,
                _ => continue,
            };
            *slot = match value.parse::<i64>() {
                Ok(n) if n < 0 => Some(0),
                Ok(n) => u32::try_from(n).ok(),
                Err(_) => None,
            };
        }
        password_policy(min_length, max_age_days, lockout_threshold)
    }

    /// One `/etc/shadow` row, reduced to the facts governance needs.
    ///
    /// Parsed in the portable core so the discrimination rules below are unit-tested
    /// on every platform, including Windows CI — the shadow file itself is only ever
    /// read by the Linux arm.
    #[derive(Debug, Clone, PartialEq, Eq)]
    // The parsers below read Linux-only files, so off Linux their only consumers
    // are the unit tests — which is the point of keeping them here rather than in
    // `linux_impl`: the discrimination rules are exercised on every platform,
    // including Windows CI. The mirror of the `#[cfg(windows)]`-only items above.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub struct ShadowFacts {
        /// `false` only for a genuinely password-less account (empty hash field).
        /// A locked (`!`) or disabled (`*`) hash still means a password is required —
        /// the account simply cannot authenticate with it.
        pub password_required: bool,
        /// RFC3339 UTC of the last password change; `None` when never set.
        pub password_last_set: Option<String>,
        /// The account-expiry date has passed. This — not the password lock — is what
        /// blocks every sign-in path including SSH public keys (ADR-0047).
        pub expired: bool,
    }

    /// Parse `/etc/shadow` into `name → facts`.
    ///
    /// `today_days` is the current day number since the Unix epoch, passed in so the
    /// expiry comparison is testable. Field layout is
    /// `name:hash:lastchg:min:max:warn:inactive:expire:reserved`; a row with fewer
    /// fields is skipped rather than guessed at.
    ///
    /// An expiry of `0` is treated as *not* expired: `useradd` writes it for "no
    /// expiry" on some distros, and reading 1970-01-01 as a deliberate lockout would
    /// report healthy accounts as suspended.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn parse_shadow(
        text: &str,
        today_days: i64,
    ) -> std::collections::BTreeMap<String, ShadowFacts> {
        let mut out = std::collections::BTreeMap::new();
        for line in text.lines() {
            let cols: Vec<&str> = line.split(':').collect();
            if cols.len() < 8 || cols[0].trim().is_empty() {
                continue;
            }
            let hash = cols[1];
            let last_change: Option<i64> = cols[2].trim().parse().ok();
            let expire: Option<i64> = cols[7].trim().parse().ok();
            out.insert(
                cols[0].trim().to_string(),
                ShadowFacts {
                    password_required: !hash.is_empty(),
                    password_last_set: match last_change {
                        Some(days) if days > 0 && !hash.is_empty() => days_to_rfc3339(days),
                        _ => None,
                    },
                    expired: matches!(expire, Some(days) if days > 0 && days <= today_days),
                },
            );
        }
        out
    }

    /// Day number since the Unix epoch → RFC3339 UTC midnight.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    fn days_to_rfc3339(days: i64) -> Option<String> {
        let secs = days.checked_mul(86_400)?;
        chrono::DateTime::from_timestamp(secs, 0)
            .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }

    /// Read a numeric setting out of a `key = value` / `key value` config file.
    ///
    /// One parser for `/etc/security/pwquality.conf` (`minlen = 8`),
    /// `/etc/login.defs` (`PASS_MAX_DAYS   99999`) and
    /// `/etc/security/faillock.conf` (`deny = 3`) — the three formats differ only in
    /// whitespace. Comments (`#`) and non-numeric values are ignored; the **last**
    /// occurrence wins, which is how all three files are evaluated.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn parse_kv_setting(text: &str, key: &str) -> Option<u32> {
        let mut found = None;
        for line in text.lines() {
            let line = line.split('#').next().unwrap_or("").trim();
            let Some(rest) = line.strip_prefix(key) else {
                continue;
            };
            // Guard against `minlen_extra = 3` matching the key `minlen`.
            if !rest.is_empty() && !rest.starts_with(|c: char| c.is_whitespace() || c == '=') {
                continue;
            }
            let value = rest
                .trim_start_matches(|c: char| c.is_whitespace() || c == '=')
                .trim();
            if let Ok(n) = value.parse::<u32>() {
                found = Some(n);
            }
        }
        found
    }

    /// Users and groups granted `sudo` by a rule in `/etc/sudoers`(`.d`).
    ///
    /// Deliberately shallow: direct `user ALL=…` and `%group ALL=…` lines only. No
    /// alias resolution, no `#include` chasing, no host/runas matching. It exists to
    /// answer one question honestly — *might this account be an administrator by a
    /// route kenny cannot revoke?* — and a false positive there costs a greyed-out
    /// button, while a false negative costs a governance call that reports success
    /// and changes nothing (ADR-0047). kenny never writes these files.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn parse_sudoers(text: &str) -> (Vec<String>, Vec<String>) {
        let (mut users, mut groups) = (Vec::new(), Vec::new());
        for line in text.lines() {
            let line = line.split('#').next().unwrap_or("").trim();
            // A rule is `<who> <hosts>=(<runas>) <commands>`; we only need <who>.
            let Some((who, rest)) = line.split_once(|c: char| c.is_whitespace()) else {
                continue;
            };
            if !rest.contains('=') || who.is_empty() {
                continue;
            }
            // Skip `Defaults`, `User_Alias`, `Cmnd_Alias`, … — they are not grants.
            if who.chars().next().is_some_and(|c| c.is_uppercase()) && !who.starts_with('%') {
                continue;
            }
            match who.strip_prefix('%') {
                Some(group) => groups.push(group.trim_start_matches('#').to_string()),
                None => users.push(who.to_string()),
            }
        }
        (users, groups)
    }

    /// Sort accounts by name and derive the `admins` list (enabled *and* disabled
    /// admin names, sorted). Returns `(accounts, admins, count)`.
    ///
    /// Each account's `unsupported` map is merged from three layers, least specific
    /// first, so the most specific reason wins: **host** ([`HostCaps`]) → **kind**
    /// ([`Kind::unsupported`]) → **account** (`Account::extra_unsupported`). See
    /// ADR-0047. `serde_json::Map` is a `BTreeMap`, so the merged key order is
    /// deterministic regardless of insertion order — fixtures stay stable.
    pub fn shape(mut accounts: Vec<Account>, host: &HostCaps) -> (Vec<Value>, Vec<String>, usize) {
        accounts.sort_by(|a, b| a.name.cmp(&b.name));
        let admins: Vec<String> = accounts
            .iter()
            .filter(|a| a.is_admin)
            .map(|a| a.name.clone())
            .collect();
        let count = accounts.len();
        let out = accounts
            .into_iter()
            .map(|a| {
                let unsupported: serde_json::Map<String, Value> = host
                    .negations()
                    .iter()
                    .copied()
                    .chain(a.kind.unsupported())
                    .chain(a.extra_unsupported.iter().copied())
                    .map(|(verb, reason)| (verb.to_string(), Value::from(reason)))
                    .collect();
                json!({
                    "name": a.name,
                    "display": a.display,
                    "kind": a.kind.as_str(),
                    "enabled": a.enabled,
                    "is_admin": a.is_admin,
                    "password_required": a.password_required,
                    "password_last_set": a.password_last_set,
                    "last_logon": a.last_logon,
                    "builtin_admin": a.builtin_admin,
                    "builtin_guest": a.builtin_guest,
                    "deny_logon": a.deny_logon,
                    "unsupported": unsupported,
                })
            })
            .collect();
        (out, admins, count)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn account(name: &str, enabled: bool, is_admin: bool) -> Account {
            Account {
                name: name.to_string(),
                display: name.to_string(),
                kind: Kind::Local,
                deny_logon: Vec::new(),
                enabled,
                is_admin,
                password_required: true,
                password_last_set: None,
                last_logon: None,
                builtin_admin: false,
                builtin_guest: false,
                extra_unsupported: Vec::new(),
            }
        }

        #[test]
        fn from_row_parses_and_defaults_booleans() {
            let row = json!({
                "name": "kid", "enabled": true, "is_admin": false,
                "password_required": true, "last_logon": "2026-06-04T15:02:00Z",
                "password_last_set": "2026-02-20T18:30:00Z",
                "builtin_admin": false, "builtin_guest": false
            });
            let a = Account::from_row(&row).unwrap();
            assert_eq!(a.name, "kid");
            assert!(a.enabled);
            assert!(!a.is_admin);
            assert!(a.password_required);
            assert_eq!(a.last_logon.as_deref(), Some("2026-06-04T15:02:00Z"));
            assert_eq!(a.password_last_set.as_deref(), Some("2026-02-20T18:30:00Z"));
            // Missing booleans default to false; missing name drops the row.
            let a = Account::from_row(&json!({ "name": "Guest" })).unwrap();
            assert!(!a.enabled);
            assert_eq!(a.last_logon, None);
            assert_eq!(a.password_last_set, None);
            assert!(Account::from_row(&json!({ "enabled": true })).is_none());
        }

        #[test]
        fn shape_sorts_and_lists_admins_including_disabled() {
            let mut disabled_admin = account("old-admin", false, true);
            disabled_admin.builtin_admin = true;
            let (out, admins, count) = shape(
                vec![
                    account("papa", true, true),
                    account("kid", true, false),
                    disabled_admin,
                ],
                &HostCaps::none(),
            );
            assert_eq!(count, 3);
            assert_eq!(out[0]["name"], "kid");
            assert_eq!(out[1]["name"], "old-admin");
            assert_eq!(out[1]["builtin_admin"], true);
            assert_eq!(out[2]["name"], "papa");
            // Disabled admins are still admins (contract: enabled + disabled names).
            assert_eq!(admins, vec!["old-admin".to_string(), "papa".to_string()]);
            // No SID-shaped strings anywhere in the payload.
            for a in &out {
                assert!(a.get("sid").is_none());
                assert!(!a.to_string().contains("S-1-5-"));
            }
            // The nullable field serializes even when null (key present as null).
            assert!(out[0].get("password_last_set").is_some());
        }

        #[test]
        fn kind_maps_principal_source_and_refuses_to_guess() {
            assert_eq!(Kind::from_principal_source(Some("Local")), Kind::Local);
            assert_eq!(
                Kind::from_principal_source(Some("MicrosoftAccount")),
                Kind::Microsoft
            );
            assert_eq!(Kind::from_principal_source(Some("AzureAD")), Kind::Entra);
            // An unreadable or unfamiliar source must not become `Local` — that
            // would advertise reset_password as supported when it is not.
            assert_eq!(Kind::from_principal_source(None), Kind::Unknown);
            assert_eq!(Kind::from_principal_source(Some("")), Kind::Unknown);
            assert_eq!(Kind::from_principal_source(Some("Whatever")), Kind::Unknown);
        }

        #[test]
        fn unsupported_is_a_negation_local_is_fully_capable() {
            // Every governance verb is available unless listed. A local account
            // lists nothing at all — that is the point of the whole design.
            assert!(Kind::Local.unsupported().is_empty());
            assert_eq!(
                Kind::Microsoft.unsupported(),
                vec![("reset_password", "password_in_cloud")]
            );
            assert_eq!(
                Kind::Entra.unsupported(),
                vec![("reset_password", "password_in_cloud")]
            );
            assert_eq!(
                Kind::Unknown.unsupported(),
                vec![("reset_password", "kind_unknown")]
            );
            // The verbs kenny actually ships are supported for every kind.
            for kind in [Kind::Local, Kind::Microsoft, Kind::Entra, Kind::Unknown] {
                let blocked: Vec<&str> = kind.unsupported().into_iter().map(|(v, _)| v).collect();
                for verb in ["set_enabled", "set_admin", "set_logon_rights", "delete"] {
                    assert!(!blocked.contains(&verb), "{verb} must work for {kind:?}");
                }
            }
        }

        #[test]
        fn display_never_carries_a_microsoft_address() {
            // The two shapes an MSA identity takes, both rejected in favour of the
            // SAM name (ADR-0046).
            assert_eq!(display_label(Some("kid@outlook.com"), "kid"), "kid");
            assert_eq!(
                display_label(Some("MicrosoftAccount\\kid@outlook.com"), "kid"),
                "kid"
            );
            assert_eq!(display_label(Some("  "), "kid"), "kid");
            assert_eq!(display_label(None, "kid"), "kid");
            assert_eq!(display_label(Some(" Kid "), "kid"), "Kid");
        }

        #[test]
        fn from_row_reads_governance_fields_and_filters_deny_rights() {
            let row = json!({
                "name": "kid", "display": "Kid",
                "principal_source": "MicrosoftAccount",
                // An unknown right from a future/edited probe is dropped rather
                // than echoed onto the wire.
                "deny_logon": ["remote_interactive", "interactive"],
                "enabled": true, "is_admin": false, "password_required": true
            });
            let a = Account::from_row(&row).unwrap();
            assert_eq!(a.kind, Kind::Microsoft);
            assert_eq!(a.display, "Kid");
            assert_eq!(a.deny_logon, vec!["remote_interactive".to_string()]);

            // Missing governance fields degrade to the restrictive/empty case.
            let bare = Account::from_row(&json!({ "name": "svc" })).unwrap();
            assert_eq!(bare.kind, Kind::Unknown);
            assert_eq!(bare.display, "svc");
            assert!(bare.deny_logon.is_empty());
        }

        #[test]
        fn shape_emits_kind_and_unsupported_per_account() {
            let mut msa = account("kid", true, false);
            msa.kind = Kind::Microsoft;
            msa.display = "Kid".to_string();
            msa.deny_logon = vec!["network".to_string()];
            let (out, _, _) = shape(vec![msa, account("papa", true, true)], &HostCaps::none());

            assert_eq!(out[0]["kind"], "microsoft");
            assert_eq!(out[0]["display"], "Kid");
            assert_eq!(out[0]["deny_logon"], json!(["network"]));
            assert_eq!(out[0]["unsupported"]["reset_password"], "password_in_cloud");
            // A local account carries an empty map, not a missing key: consumers
            // read `unsupported` unconditionally.
            assert_eq!(out[1]["kind"], "local");
            assert_eq!(out[1]["unsupported"], json!({}));

            // Still no SIDs, and now no Microsoft addresses either.
            for a in &out {
                let dumped = a.to_string();
                assert!(!dumped.contains("S-1-5-"));
                assert!(!dumped.contains('@'));
            }
        }

        #[test]
        fn parse_system_access_reads_locale_invariant_keys() {
            let inf = "\
MinimumPasswordAge = 0
MaximumPasswordAge = 42
MinimumPasswordLength = 8
LockoutBadCount = 10
PasswordComplexity = 1
";
            let p = parse_system_access(inf.lines());
            assert_eq!(p["applies_to"], "local_only");
            assert_eq!(p["min_length"], 8);
            assert_eq!(p["max_age_days"], 42);
            assert_eq!(p["lockout_threshold"], 10);

            // "Never expires" is -1 in the INF; Windows shows it as 0.
            let p = parse_system_access(["MaximumPasswordAge = -1"].into_iter());
            assert_eq!(p["max_age_days"], 0);

            // An unreadable export leaves nulls, not zeros: "no minimum" and "could
            // not read the minimum" must not look alike to a health rule.
            let p = parse_system_access(std::iter::empty());
            assert!(p["min_length"].is_null());
            assert!(p["lockout_threshold"].is_null());
            assert_eq!(p["applies_to"], "local_only");
        }

        #[test]
        fn unsupported_merges_host_kind_and_account_most_specific_wins() {
            let mut host = HostCaps::none();
            host.gap("deny_network", "no_network_logon_concept");
            host.gap("session_lock", "no_graphical_session");

            let mut root = account("root", true, true);
            root.builtin_admin = true;
            root.extra_unsupported = vec![
                ("set_admin", "root_account"),
                // The account layer overrides the host layer for the same verb.
                ("session_lock", "root_account"),
            ];

            let mut msa = account("kid", true, false);
            msa.kind = Kind::Microsoft;

            let (out, _, _) = shape(vec![root, msa], &host);

            // kid: host gaps + kind gap, no account gaps.
            assert_eq!(out[0]["name"], "kid");
            assert_eq!(
                out[0]["unsupported"],
                json!({
                    "deny_network": "no_network_logon_concept",
                    "session_lock": "no_graphical_session",
                    "reset_password": "password_in_cloud",
                })
            );

            // root: host gap survives where the account is silent, and loses where
            // the account speaks.
            assert_eq!(out[1]["name"], "root");
            assert_eq!(
                out[1]["unsupported"],
                json!({
                    "deny_network": "no_network_logon_concept",
                    "session_lock": "root_account",
                    "set_admin": "root_account",
                })
            );
        }

        #[test]
        fn shadow_distinguishes_locked_expired_and_passwordless() {
            // day 20000 ≈ 2024-10-04; "today" is day 20500.
            let text = "\
root:$6$abc$def:19000:0:99999:7:::
papa:!$6$abc$def:19500:0:99999:7::0:
kid:$6$xyz$w:20000:0:99999:7::1:
ghost::19900:0:99999:7:::
future:$6$q$r:20100:0:99999:7::21000:
short:x:1
";
            let s = parse_shadow(text, 20_500);

            // A real hash: password required, change date reported.
            assert!(s["root"].password_required);
            assert_eq!(
                s["root"].password_last_set.as_deref(),
                Some("2022-01-08T00:00:00Z")
            );
            assert!(!s["root"].expired);

            // Locked (`!` prefix) is NOT the same as expired, and NOT the same as
            // password-less: an SSH key still works, which is exactly why
            // `account_set_enabled` sets an expiry as well (ADR-0047).
            assert!(s["papa"].password_required);
            assert!(!s["papa"].expired, "expire=0 means no expiry, not 1970");

            // Expiry in the past -> the account is genuinely closed.
            assert!(s["kid"].expired);

            // Empty hash: genuinely password-less, and no "last set" to report. This
            // is what makes the server's blank-password-admin crit reachable.
            assert!(!s["ghost"].password_required);
            assert_eq!(s["ghost"].password_last_set, None);

            // Expiry in the future is not expiry yet.
            assert!(!s["future"].expired);

            // A truncated row is skipped rather than half-parsed.
            assert!(!s.contains_key("short"));
        }

        #[test]
        fn kv_setting_reads_all_three_pam_file_dialects() {
            // pwquality.conf
            assert_eq!(
                parse_kv_setting("# minlen = 99\nminlen = 12\n", "minlen"),
                Some(12)
            );
            // login.defs (whitespace separated)
            assert_eq!(
                parse_kv_setting("PASS_MIN_DAYS\t0\nPASS_MAX_DAYS\t90\n", "PASS_MAX_DAYS"),
                Some(90)
            );
            // faillock.conf
            assert_eq!(parse_kv_setting("deny=3\n", "deny"), Some(3));
            // Last occurrence wins, matching how the files are evaluated.
            assert_eq!(parse_kv_setting("deny = 3\ndeny = 5\n", "deny"), Some(5));
            // A longer key must not match a shorter one.
            assert_eq!(parse_kv_setting("minlen_words = 3\n", "minlen"), None);
            // Absent stays absent — "not configured" must not read as 0.
            assert_eq!(parse_kv_setting("", "deny"), None);
        }

        #[test]
        fn sudoers_scan_finds_grants_and_ignores_directives() {
            let text = "\
Defaults env_reset
User_Alias ADMINS = alice
root ALL=(ALL:ALL) ALL
%sudo   ALL=(ALL:ALL) ALL
deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl
# papa ALL=(ALL) ALL
%wheel ALL=(ALL) ALL
";
            let (users, groups) = parse_sudoers(text);
            assert_eq!(users, vec!["root".to_string(), "deploy".to_string()]);
            assert_eq!(groups, vec!["sudo".to_string(), "wheel".to_string()]);
            // Commented-out grants are not grants.
            assert!(!users.contains(&"papa".to_string()));
        }

        #[test]
        fn password_policy_omits_the_gap_map_when_there_are_no_gaps() {
            // The Windows payload must stay byte-identical: an empty map is not the
            // same as an absent key for a fixture round-trip.
            let p = password_policy(Some(8), Some(0), Some(10));
            assert!(p.get("unsupported").is_none());

            let p = password_policy_with_gaps(
                Some(8),
                None,
                None,
                &[("lockout_threshold", "pam_faillock_not_enabled")],
            );
            assert_eq!(
                p["unsupported"]["lockout_threshold"],
                "pam_faillock_not_enabled"
            );
            // A gap and a null are different claims: "no knob" vs "not read".
            assert!(p["lockout_threshold"].is_null());
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-LocalUser` joined by SID with the Administrators group and with the LSA
    /// deny-logon rights exported by `secedit`. The SIDs are compared inside the
    /// probe and reduced to booleans/short tokens — they never leave it.
    ///
    /// `PrincipalSource` is what makes a Microsoft account visible *as* a Microsoft
    /// account; the rest of the row is identical for every kind, which is the whole
    /// point (ADR-0046). `FullName` supplies `display`; the probe never emits the
    /// `MicrosoftAccount\<address>` qualified name, so no email address can leak
    /// through this path.
    pub fn collect() -> Section {
        let script = r#"
$adminSids = @()
try {
  # Well-known Administrators group SID; -SID is locale-proof. Get-LocalGroupMember
  # can throw on orphaned SIDs, hence best-effort.
  $adminSids = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
    ForEach-Object { [string]$_.SID.Value })
} catch {}

# LSA account rights and the machine password policy are not exposed by any cmdlet;
# export the security policy once and read both out of the INF. Its key names
# (SeDeny*, MinimumPasswordLength, …) are locale-invariant, unlike `net accounts`
# output. Best-effort — an unreadable export yields empty deny sets and a null
# policy, never a lost section.
$denyNetwork = @(); $denyRemote = @(); $systemAccess = @()
try {
  $cfg = Join-Path $env:TEMP ('kenny-secpol-' + [guid]::NewGuid().ToString('N') + '.inf')
  $null = & secedit.exe /export /areas USER_RIGHTS SECURITYPOLICY /cfg $cfg /quiet 2>$null
  if (Test-Path $cfg) {
    $inSystemAccess = $false
    foreach ($line in (Get-Content -LiteralPath $cfg -Encoding Unicode -ErrorAction SilentlyContinue)) {
      if ($line -match '^\s*\[') { $inSystemAccess = ($line -match '^\s*\[System Access\]') ; continue }
      if ($inSystemAccess) { $systemAccess += [string]$line; continue }
      if ($line -match '^\s*SeDenyNetworkLogonRight\s*=\s*(.*)$') {
        $denyNetwork = @($matches[1] -split ',' | ForEach-Object { $_.Trim().TrimStart('*') } | Where-Object { $_ })
      } elseif ($line -match '^\s*SeDenyRemoteInteractiveLogonRight\s*=\s*(.*)$') {
        $denyRemote = @($matches[1] -split ',' | ForEach-Object { $_.Trim().TrimStart('*') } | Where-Object { $_ })
      }
    }
    Remove-Item -LiteralPath $cfg -Force -ErrorAction SilentlyContinue
  }
} catch {}

$out = @()
Get-LocalUser -ErrorAction SilentlyContinue | ForEach-Object {
  $sid = [string]$_.SID.Value
  $deny = @()
  if ($denyNetwork -contains $sid) { $deny += 'network' }
  if ($denyRemote -contains $sid) { $deny += 'remote_interactive' }
  $out += [pscustomobject]@{
    name = [string]$_.Name
    display = [string]$_.FullName
    principal_source = [string]$_.PrincipalSource
    deny_logon = $deny
    enabled = [bool]$_.Enabled
    is_admin = ($adminSids -contains $sid)
    password_required = [bool]$_.PasswordRequired
    last_logon = if ($_.LastLogon) { (Get-Date $_.LastLogon).ToUniversalTime().ToString('o') } else { $null }
    password_last_set = if ($_.PasswordLastSet) { (Get-Date $_.PasswordLastSet).ToUniversalTime().ToString('o') } else { $null }
    builtin_admin = $sid.EndsWith('-500')
    builtin_guest = $sid.EndsWith('-501')
  }
}
ConvertTo-Json -Compress -Depth 4 ([pscustomobject]@{ accounts = @($out); system_access = @($systemAccess) })
"#;

        let probe = winps::run_json(script).unwrap_or(json!({}));
        let rows = probe
            .get("accounts")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default();
        // `[System Access]` carries no SIDs, so the parse lives in the portable core
        // where it is unit-tested on Linux CI.
        let policy = core::parse_system_access(
            probe
                .get("system_access")
                .and_then(serde_json::Value::as_array)
                .map(|lines| lines.iter().filter_map(serde_json::Value::as_str))
                .into_iter()
                .flatten(),
        );
        let accounts: Vec<core::Account> =
            rows.iter().filter_map(core::Account::from_row).collect();
        // Windows can do every governance verb the catalog names, so there are no
        // host-level gaps and the payload is unchanged from v0.15 (ADR-0047).
        let (accounts, admins, count) = core::shape(accounts, &core::HostCaps::none());

        let n_admins = admins.len();
        let plural = if n_admins == 1 { "" } else { "s" };
        Section::with_fields(
            Status::Ok,
            format!("{count} accounts, {n_admins} admin{plural}"),
            json!({
                "accounts": accounts,
                "admins": admins,
                "count": count,
                "password_policy": policy,
            }),
        )
    }
}

#[cfg(target_os = "linux")]
pub mod linux_impl {
    use super::*;
    use std::collections::HashSet;
    use std::path::Path;

    /// Shells that mean "no interactive login" — such accounts report `enabled=false`.
    const NOLOGIN_SHELLS: &[&str] = &[
        "/usr/sbin/nologin",
        "/sbin/nologin",
        "/bin/false",
        "/usr/bin/false",
    ];

    /// Groups whose members hold administrative (root-equivalent) rights, in the
    /// order kenny prefers when it has to *pick* one to promote into.
    ///
    /// Debian/Ubuntu ship `sudo`, RHEL/Fedora/Arch/SUSE ship `wheel`. Choosing the
    /// first that actually exists in `/etc/group` is the deterministic, distro-proof
    /// analogue of resolving the Windows Administrators group by its well-known SID
    /// (ADR-0047). Demotion strips membership in *all* of them, because promoting
    /// into the "wrong" one is harmless while demoting from only one is not.
    pub const ADMIN_GROUPS: &[&str] = &["sudo", "wheel", "admin"];

    pub const SSHD_CONFIG: &str = "/etc/ssh/sshd_config";
    pub const SSHD_DROPIN_DIR: &str = "/etc/ssh/sshd_config.d";
    /// kenny's own sshd drop-in. Owned entirely by kenny: it is rewritten wholesale
    /// on every `account_set_logon_rights` call and contains nothing else, so a
    /// mistake here can never damage an operator's own sshd configuration.
    pub const SSHD_DROPIN: &str = "/etc/ssh/sshd_config.d/50-kenny-deny.conf";

    /// Collect the usernames belonging to any admin group from `/etc/group`.
    ///
    /// Group lines are `name:x:gid:member1,member2,...`.
    fn admin_members(group: &str) -> HashSet<String> {
        let mut members = HashSet::new();
        for line in group.lines() {
            let mut cols = line.split(':');
            let name = cols.next().unwrap_or("");
            if !ADMIN_GROUPS.contains(&name) {
                continue;
            }
            // gid is column 3; members are column 4.
            let list = cols.nth(2).unwrap_or("");
            for member in list.split(',').map(str::trim).filter(|s| !s.is_empty()) {
                members.insert(member.to_string());
            }
        }
        members
    }

    /// The admin group kenny would promote into on this host, if any exists.
    pub fn preferred_admin_group(group: &str) -> Option<&'static str> {
        let present: HashSet<&str> = group
            .lines()
            .filter_map(|line| line.split(':').next())
            .collect();
        ADMIN_GROUPS.iter().copied().find(|g| present.contains(*g))
    }

    /// Which admin groups a user is actually in — what demotion has to strip.
    pub fn admin_groups_of(group: &str, user: &str) -> Vec<&'static str> {
        let mut out = Vec::new();
        for line in group.lines() {
            let mut cols = line.split(':');
            let name = cols.next().unwrap_or("");
            let Some(&known) = ADMIN_GROUPS.iter().find(|g| **g == name) else {
                continue;
            };
            let list = cols.nth(2).unwrap_or("");
            if list.split(',').map(str::trim).any(|m| m == user) {
                out.push(known);
            }
        }
        out
    }

    /// Users named in `DenyUsers` lines of an sshd config fragment.
    ///
    /// Shared with the `account_set_logon_rights` handler so the state kenny writes
    /// and the state it reports are parsed by the same code.
    pub fn parse_deny_users(text: &str) -> Vec<String> {
        let mut out = Vec::new();
        for line in text.lines() {
            let line = line.split('#').next().unwrap_or("").trim();
            let Some(rest) = line.strip_prefix("DenyUsers") else {
                continue;
            };
            if !rest.starts_with(|c: char| c.is_whitespace()) {
                continue;
            }
            for user in rest.split_whitespace() {
                if !out.iter().any(|u| u == user) {
                    out.push(user.to_string());
                }
            }
        }
        out
    }

    /// Does `sshd_config` pull in the drop-in directory kenny writes to?
    ///
    /// Older distros have no `Include` line, and kenny deliberately **does not add
    /// one**: editing the main config to make its own feature work is exactly the
    /// kind of change that leaves an unreachable box behind (ADR-0047).
    fn sshd_includes_dropin_dir(config: &str) -> bool {
        config.lines().any(|line| {
            let line = line.split('#').next().unwrap_or("").trim();
            line.strip_prefix("Include")
                .is_some_and(|rest| rest.contains("sshd_config.d"))
        })
    }

    /// Is a graphical session running anywhere on this host?
    ///
    /// Probed rather than declared: `account_session_action`'s `lock` needs a desktop
    /// to lock, and the same binary serves a headless NAS and a Linux desktop
    /// (ADR-0035's "clients and servers, equally"). An X socket or a Wayland socket
    /// in a per-user runtime directory is the cheapest honest evidence; neither
    /// spawns a process on every snapshot.
    fn graphical_session_present() -> bool {
        if std::fs::read_dir("/tmp/.X11-unix").is_ok_and(|mut d| d.next().is_some()) {
            return true;
        }
        std::fs::read_dir("/run/user").is_ok_and(|dirs| {
            dirs.filter_map(Result::ok).any(|user_dir| {
                std::fs::read_dir(user_dir.path()).is_ok_and(|entries| {
                    entries.filter_map(Result::ok).any(|e| {
                        e.file_name()
                            .to_str()
                            .is_some_and(|n| n.starts_with("wayland-"))
                    })
                })
            })
        })
    }

    /// `systemd-logind` creates these when it starts; their absence means there is no
    /// session manager to enumerate, lock or terminate through.
    pub fn logind_present() -> bool {
        Path::new("/run/systemd/seats").exists() || Path::new("/run/systemd/sessions").exists()
    }

    /// The last-login timestamp for `uid`, from the fixed-record `/var/log/lastlog`.
    ///
    /// Records are 292 bytes indexed by uid: `int32 time`, `char line[32]`,
    /// `char host[256]`. Best-effort — distros migrating to `lastlog2` have no such
    /// file, and a never-logged-in account has a zero record. Reporting `null` in
    /// both cases matches what Windows does for an account that never signed in.
    fn last_logon(lastlog: &[u8], uid: u32) -> Option<String> {
        const RECORD: usize = 292;
        let start = (uid as usize).checked_mul(RECORD)?;
        let secs = lastlog.get(start..start + 4)?;
        let secs = i64::from(u32::from_ne_bytes([secs[0], secs[1], secs[2], secs[3]]));
        if secs == 0 {
            return None;
        }
        chrono::DateTime::from_timestamp(secs, 0)
            .map(|dt| dt.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }

    /// Everything the collector reads off disk, in one struct so `parse_accounts` is
    /// a pure function and testable without a filesystem.
    pub struct Sources<'a> {
        pub passwd: &'a str,
        pub group: &'a str,
        /// `None` when `/etc/shadow` could not be read — the agent is not running as
        /// root. CI's e2e job runs exactly that way, so this path is exercised on
        /// every build rather than being theoretical.
        pub shadow: Option<&'a str>,
        pub sudoers_users: &'a [String],
        pub sudoers_groups: &'a [String],
        pub ssh_denied: &'a [String],
        pub lastlog: &'a [u8],
        pub today_days: i64,
    }

    /// Parse the sources into accounts.
    ///
    /// Keeps root (uid 0) and human accounts (uid >= 1000), skipping the `nobody`
    /// placeholder (uid 65534) — so the Linux panel is not padded with two dozen
    /// service accounts the operator can do nothing about.
    ///
    /// `enabled` is **login shell AND not expired**. A locked password deliberately
    /// does not flip it: `usermod -L` leaves SSH public-key authentication working,
    /// so reporting such an account as suspended would be the most dangerous kind of
    /// wrong. Account expiry is what actually closes every door (ADR-0047).
    pub fn parse_accounts(src: &Sources<'_>) -> Vec<core::Account> {
        let admins = admin_members(src.group);
        let shadow = src
            .shadow
            .map(|text| core::parse_shadow(text, src.today_days))
            .unwrap_or_default();
        let sudoers_group_members: HashSet<String> = src
            .sudoers_groups
            .iter()
            .flat_map(|g| group_members(src.group, g))
            .collect();

        src.passwd
            .lines()
            .filter_map(|line| {
                let mut cols = line.split(':');
                let name = cols.next().unwrap_or("").trim();
                let _passwd = cols.next()?; // "x"
                let uid: u32 = cols.next()?.trim().parse().ok()?;
                let _gid = cols.next()?;
                let gecos = cols.next()?;
                let _home = cols.next()?;
                let shell = cols.next().unwrap_or("").trim();
                if name.is_empty() {
                    return None;
                }
                if !is_reportable_uid(uid) {
                    return None;
                }

                let login_shell = !shell.is_empty() && !NOLOGIN_SHELLS.contains(&shell);
                let facts = shadow.get(name);
                let expired = facts.is_some_and(|f| f.expired);
                let is_root = uid == 0;
                let via_sudoers = src.sudoers_users.iter().any(|u| u == name)
                    || sudoers_group_members.contains(name);
                let in_admin_group = admins.contains(name);

                let mut extra_unsupported = Vec::new();
                if is_root {
                    // Group membership neither grants nor revokes root, and expiring
                    // root's account on a headless box is how you lose the machine.
                    extra_unsupported.push(("set_admin", "root_account"));
                    extra_unsupported.push(("set_enabled", "root_account"));
                } else if via_sudoers && !in_admin_group {
                    // kenny can see this grant but will not edit sudoers to remove
                    // it. Saying so beats reporting success and changing nothing.
                    extra_unsupported.push(("set_admin", "admin_via_sudoers"));
                }
                if !login_shell {
                    // Clearing an expiry would not make this account usable, and
                    // rewriting a login shell is a different and more surprising act.
                    extra_unsupported.push(("set_enabled", "nologin_shell"));
                }

                Some(core::Account {
                    name: name.to_string(),
                    // Linux has no PrincipalSource; every /etc/passwd entry is local
                    // by construction, and GECOS supplies the display label.
                    display: core::display_label(gecos.split(',').next(), name),
                    kind: core::Kind::Local,
                    deny_logon: if src.ssh_denied.iter().any(|u| u == name) {
                        vec!["remote_interactive".to_string()]
                    } else {
                        Vec::new()
                    },
                    enabled: login_shell && !expired,
                    is_admin: is_root || in_admin_group || via_sudoers,
                    // Without /etc/shadow kenny cannot tell, and the safe answer is
                    // "a password is required" — claiming otherwise would raise a
                    // blank-password crit on every unprivileged dev build.
                    password_required: facts.map(|f| f.password_required).unwrap_or(true),
                    password_last_set: facts.and_then(|f| f.password_last_set.clone()),
                    last_logon: last_logon(src.lastlog, uid),
                    // root is the built-in, undeletable administrator — the same fact
                    // `builtin_admin` records for Windows RID 500, so the portable
                    // guard protects it with no Linux-specific rule.
                    builtin_admin: is_root,
                    builtin_guest: false,
                    extra_unsupported,
                })
            })
            .collect()
    }

    /// Is this `/etc/passwd` uid one kenny reports as an account?
    ///
    /// The single definition of "an account on this machine" for the Linux arm —
    /// root plus real users, without the two dozen service entries an operator can
    /// do nothing about. `logon_failures` matches against the same set, so an
    /// attempt against a service account collapses into `unmatched_count` rather
    /// than appearing as a named row the accounts panel does not list.
    pub fn is_reportable_uid(uid: u32) -> bool {
        uid != 65534 && (uid == 0 || uid >= 1000)
    }

    /// The names of the accounts kenny reports, straight from `/etc/passwd`.
    pub fn login_account_names(passwd: &str) -> Vec<String> {
        passwd
            .lines()
            .filter_map(|line| {
                let mut cols = line.split(':');
                let name = cols.next().unwrap_or("").trim();
                let _passwd = cols.next()?;
                let uid: u32 = cols.next()?.trim().parse().ok()?;
                (!name.is_empty() && is_reportable_uid(uid)).then(|| name.to_string())
            })
            .collect()
    }

    /// Members of one group from `/etc/group` (secondary membership only).
    fn group_members(group: &str, wanted: &str) -> Vec<String> {
        for line in group.lines() {
            let mut cols = line.split(':');
            if cols.next().unwrap_or("") != wanted {
                continue;
            }
            return cols
                .nth(2)
                .unwrap_or("")
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect();
        }
        Vec::new()
    }

    /// What this host cannot do, regardless of which account is asked about.
    pub fn host_caps(group: &str, shadow_readable: bool) -> core::HostCaps {
        let mut caps = core::HostCaps::none();

        // Windows separates SMB/network logon from RDP; Linux has one remote
        // sign-in plane and it is SSH. There is nothing to deny here, and inventing
        // a mapping would be worse than saying so.
        caps.gap("deny_network", "no_network_logon_concept");

        let sshd_config = std::fs::read_to_string(SSHD_CONFIG).ok();
        match sshd_config {
            None => caps.gap("deny_remote_interactive", "no_sshd"),
            Some(config) if !sshd_includes_dropin_dir(&config) => {
                caps.gap("deny_remote_interactive", "sshd_no_include")
            }
            Some(_) => {}
        }

        if !logind_present() {
            caps.gap("session_lock", "no_logind");
            caps.gap("session_logoff", "no_logind");
        } else if !graphical_session_present() {
            caps.gap("session_lock", "no_graphical_session");
        }

        // There is no portable way to show a message to a signed-in user before
        // acting: `wall` is machine-wide, `write` needs a tty with `mesg y`, and
        // `notify-send` needs the user's own D-Bus session bus. Rather than send a
        // warning that may vanish, kenny reports that it cannot warn.
        caps.gap("session_warn", "no_user_notification_channel");

        if preferred_admin_group(group).is_none() {
            caps.gap("set_admin", "no_admin_group");
        }
        if !shadow_readable {
            // The agent is not running as root (a dev build, or CI). Governance
            // writes would fail anyway, and `enabled` is only shell-deep.
            caps.gap("set_enabled", "shadow_unreadable");
        }
        caps
    }

    /// Read the machine-wide password policy from the PAM/shadow-suite config files.
    ///
    /// `min_length` comes from `pwquality.conf`, **not** `login.defs PASS_MIN_LEN` —
    /// PAM ignores the latter, so reporting it would describe a rule nothing
    /// enforces. Each file that is absent becomes a `null` value plus an entry in
    /// the policy's own `unsupported` map, so "not configured" and "this host has no
    /// such knob" stay distinguishable (ADR-0047).
    fn password_policy() -> serde_json::Value {
        let pwquality = std::fs::read_to_string("/etc/security/pwquality.conf").ok();
        let login_defs = std::fs::read_to_string("/etc/login.defs").unwrap_or_default();
        let faillock = std::fs::read_to_string("/etc/security/faillock.conf").ok();

        let mut gaps: Vec<(&'static str, &'static str)> = Vec::new();
        let min_length = match &pwquality {
            Some(text) => core::parse_kv_setting(text, "minlen"),
            None => {
                gaps.push(("min_length", "no_pwquality"));
                None
            }
        };
        let max_age_days = core::parse_kv_setting(&login_defs, "PASS_MAX_DAYS");
        let lockout_threshold = match (&faillock, faillock_in_pam_stack()) {
            (Some(text), true) => core::parse_kv_setting(text, "deny"),
            _ => {
                gaps.push(("lockout_threshold", "pam_faillock_not_enabled"));
                None
            }
        };
        core::password_policy_with_gaps(min_length, max_age_days, lockout_threshold, &gaps)
    }

    /// Is `pam_faillock` actually referenced by the PAM stack?
    ///
    /// `faillock.conf` existing proves the module is installed, not that anything
    /// consults it. kenny reads `/etc/pam.d` to find out and **never writes it** — a
    /// mistake in that directory locks out every form of authentication at once.
    fn faillock_in_pam_stack() -> bool {
        let Ok(entries) = std::fs::read_dir("/etc/pam.d") else {
            return false;
        };
        entries.filter_map(Result::ok).any(|entry| {
            std::fs::read_to_string(entry.path()).is_ok_and(|text| {
                text.lines().any(|l| {
                    let l = l.split('#').next().unwrap_or("");
                    l.contains("pam_faillock.so")
                })
            })
        })
    }

    /// Read every source and shape the section.
    pub fn collect() -> Section {
        let Ok(passwd) = std::fs::read_to_string("/etc/passwd") else {
            return Section::with_fields(
                Status::Ok,
                "n/a on this platform",
                json!({
                    "accounts": [], "admins": [], "count": 0,
                    "password_policy": core::password_policy(None, None, None),
                }),
            );
        };
        let group = std::fs::read_to_string("/etc/group").unwrap_or_default();
        let shadow = std::fs::read_to_string("/etc/shadow").ok();
        let lastlog = std::fs::read("/var/log/lastlog").unwrap_or_default();
        let ssh_denied = std::fs::read_to_string(SSHD_DROPIN)
            .map(|text| parse_deny_users(&text))
            .unwrap_or_default();
        let (sudoers_users, sudoers_groups) = read_sudoers();
        let today_days = chrono::Utc::now().timestamp() / 86_400;

        let accounts = parse_accounts(&Sources {
            passwd: &passwd,
            group: &group,
            shadow: shadow.as_deref(),
            sudoers_users: &sudoers_users,
            sudoers_groups: &sudoers_groups,
            ssh_denied: &ssh_denied,
            lastlog: &lastlog,
            today_days,
        });
        let caps = host_caps(&group, shadow.is_some());
        let (accounts, admins, count) = core::shape(accounts, &caps);

        let n_admins = admins.len();
        let plural = if n_admins == 1 { "" } else { "s" };
        Section::with_fields(
            Status::Ok,
            format!("{count} accounts, {n_admins} admin{plural}"),
            json!({
                "accounts": accounts, "admins": admins, "count": count,
                "password_policy": password_policy(),
            }),
        )
    }

    /// `/etc/sudoers` plus every fragment in `/etc/sudoers.d`, scanned read-only.
    fn read_sudoers() -> (Vec<String>, Vec<String>) {
        let mut text = std::fs::read_to_string("/etc/sudoers").unwrap_or_default();
        if let Ok(entries) = std::fs::read_dir("/etc/sudoers.d") {
            for entry in entries.filter_map(Result::ok) {
                if let Ok(fragment) = std::fs::read_to_string(entry.path()) {
                    text.push('\n');
                    text.push_str(&fragment);
                }
            }
        }
        core::parse_sudoers(&text)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        const PASSWD: &str = "\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
papa:x:1000:1000:Papa:/home/papa:/bin/bash
kid:x:1001:1001:Kid:/home/kid:/bin/bash
svc:x:1002:1002:Service:/home/svc:/usr/sbin/nologin
deploy:x:1003:1003:Deploy:/home/deploy:/bin/bash
";
        const GROUP: &str = "\
root:x:0:
sudo:x:27:papa
wheel:x:998:
devs:x:1100:deploy
";

        fn sources<'a>(shadow: Option<&'a str>, denied: &'a [String]) -> Sources<'a> {
            Sources {
                passwd: PASSWD,
                group: GROUP,
                shadow,
                sudoers_users: &[],
                sudoers_groups: &[],
                ssh_denied: denied,
                lastlog: &[],
                today_days: 20_500,
            }
        }

        #[test]
        fn parse_accounts_selects_users_and_admins() {
            let accounts = parse_accounts(&sources(None, &[]));
            let names: Vec<&str> = accounts.iter().map(|a| a.name.as_str()).collect();
            // System users (daemon uid 1) and nobody (65534) are excluded.
            assert_eq!(names, vec!["root", "papa", "kid", "svc", "deploy"]);

            let root = &accounts[0];
            assert!(root.is_admin, "uid 0 is admin");
            assert!(root.enabled);
            assert!(root.builtin_admin, "root is the built-in administrator");
            assert!(!root.builtin_guest);
            // Without /etc/shadow kenny assumes a password rather than raising a
            // blank-password crit on every unprivileged build.
            assert!(root.password_required);
            assert_eq!(root.password_last_set, None);

            let papa = &accounts[1];
            assert!(papa.is_admin, "member of sudo group");
            let kid = &accounts[2];
            assert!(!kid.is_admin);
            assert!(kid.enabled);
            let svc = &accounts[3];
            assert!(!svc.is_admin);
            assert!(!svc.enabled, "nologin shell disables the account");
        }

        #[test]
        fn root_publishes_the_verbs_it_cannot_perform() {
            let accounts = parse_accounts(&sources(None, &[]));
            let root = &accounts[0];
            assert!(root
                .extra_unsupported
                .contains(&("set_admin", "root_account")));
            assert!(root
                .extra_unsupported
                .contains(&("set_enabled", "root_account")));

            // And a shell-less account says why restoring it would not help.
            let svc = accounts.iter().find(|a| a.name == "svc").unwrap();
            assert!(svc
                .extra_unsupported
                .contains(&("set_enabled", "nologin_shell")));
        }

        #[test]
        fn expiry_not_password_lock_is_what_disables_an_account() {
            // papa's password is locked (`!`) but the account is not expired: an SSH
            // key still works, so `enabled` must stay true. kid is expired.
            let shadow = "\
root:$6$a$b:19000:0:99999:7:::
papa:!$6$a$b:19500:0:99999:7:::
kid:$6$a$b:20000:0:99999:7::1:
";
            let accounts = parse_accounts(&sources(Some(shadow), &[]));
            let papa = accounts.iter().find(|a| a.name == "papa").unwrap();
            let kid = accounts.iter().find(|a| a.name == "kid").unwrap();
            assert!(papa.enabled, "a locked password still admits an SSH key");
            assert!(!kid.enabled, "an expired account is closed to every path");
            assert!(papa.password_required);
            assert_eq!(
                papa.password_last_set.as_deref(),
                Some("2023-05-23T00:00:00Z")
            );
        }

        #[test]
        fn sudoers_granted_admins_are_reported_but_not_revocable() {
            let users = vec!["deploy".to_string()];
            let src = Sources {
                sudoers_users: &users,
                ..sources(None, &[])
            };
            let accounts = parse_accounts(&src);
            let deploy = accounts.iter().find(|a| a.name == "deploy").unwrap();
            assert!(deploy.is_admin, "a sudoers grant is administrator rights");
            assert!(
                deploy
                    .extra_unsupported
                    .contains(&("set_admin", "admin_via_sudoers")),
                "kenny must not offer to revoke what it will not edit"
            );

            // A group-based sudoers grant reaches the group's members too.
            let groups = vec!["devs".to_string()];
            let src = Sources {
                sudoers_groups: &groups,
                ..sources(None, &[])
            };
            let deploy = parse_accounts(&src)
                .into_iter()
                .find(|a| a.name == "deploy")
                .unwrap();
            assert!(deploy.is_admin);
        }

        #[test]
        fn ssh_denial_round_trips_through_the_dropin() {
            let denied = parse_deny_users("# kenny\nDenyUsers kid papa\nDenyUsers kid\n");
            assert_eq!(denied, vec!["kid".to_string(), "papa".to_string()]);
            // A commented-out or unrelated directive is not a denial.
            assert!(parse_deny_users("#DenyUsers kid\nDenyUsersFoo bar\n").is_empty());

            let accounts = parse_accounts(&sources(None, &denied));
            let kid = accounts.iter().find(|a| a.name == "kid").unwrap();
            assert_eq!(kid.deny_logon, vec!["remote_interactive".to_string()]);
            let papa = accounts.iter().find(|a| a.name == "papa").unwrap();
            assert_eq!(papa.deny_logon, vec!["remote_interactive".to_string()]);
            let svc = accounts.iter().find(|a| a.name == "svc").unwrap();
            assert!(svc.deny_logon.is_empty());
        }

        #[test]
        fn admin_group_resolution_prefers_sudo_then_wheel_and_demotion_strips_all() {
            assert_eq!(preferred_admin_group(GROUP), Some("sudo"));
            assert_eq!(preferred_admin_group("wheel:x:998:\n"), Some("wheel"));
            assert_eq!(preferred_admin_group("users:x:100:\n"), None);

            assert_eq!(admin_groups_of(GROUP, "papa"), vec!["sudo"]);
            assert!(admin_groups_of(GROUP, "kid").is_empty());
            // Membership in more than one must be stripped in full, or demotion is
            // a promise kenny does not keep.
            let both = "sudo:x:27:papa\nwheel:x:998:papa\n";
            assert_eq!(admin_groups_of(both, "papa"), vec!["sudo", "wheel"]);
        }

        #[test]
        fn host_caps_name_what_this_machine_cannot_do() {
            let caps = host_caps(GROUP, false);
            let gaps: Vec<&str> = caps.negations().iter().map(|(v, _)| *v).collect();
            // Always true on Linux: there is no network-logon plane, and no way to
            // warn a signed-in user first.
            assert!(gaps.contains(&"deny_network"));
            assert!(gaps.contains(&"session_warn"));
            // Not running as root.
            assert!(gaps.contains(&"set_enabled"));
            // An admin group exists in this fixture, so that is not a gap.
            assert!(!gaps.contains(&"set_admin"));
            assert!(host_caps("users:x:100:\n", true)
                .negations()
                .iter()
                .any(|(v, r)| *v == "set_admin" && *r == "no_admin_group"));
        }

        #[test]
        fn sshd_include_is_detected_never_added() {
            assert!(sshd_includes_dropin_dir(
                "Include /etc/ssh/sshd_config.d/*.conf\nPort 22\n"
            ));
            assert!(!sshd_includes_dropin_dir(
                "#Include /etc/ssh/sshd_config.d/*.conf\n"
            ));
            assert!(!sshd_includes_dropin_dir("Port 22\n"));
        }

        #[test]
        fn lastlog_reads_a_record_and_tolerates_a_missing_file() {
            let mut buf = vec![0u8; 292 * 2];
            buf[292..296].copy_from_slice(&1_700_000_000u32.to_ne_bytes());
            assert_eq!(last_logon(&buf, 1).as_deref(), Some("2023-11-14T22:13:20Z"));
            // uid 0 has a zero record: never logged in, not "logged in at epoch".
            assert_eq!(last_logon(&buf, 0), None);
            // Past the end of the file, or no file at all.
            assert_eq!(last_logon(&buf, 9999), None);
            assert_eq!(last_logon(&[], 0), None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_accounts_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["accounts"].is_array());
        assert!(v["admins"].is_array());
        assert!(v["count"].is_number());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_reports_real_accounts() {
        // The Linux arm reads /etc/passwd + /etc/group; assert the documented
        // shape without pinning a machine-specific count.
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["summary"].is_string());
        assert!(v["accounts"].is_array());
        assert!(v["admins"].is_array());
        assert!(v["count"].is_number());
        assert_eq!(v["password_policy"]["applies_to"], "local_only");
        // CI runs the agent as an unprivileged user, so this permanently
        // exercises the degradation path: no /etc/shadow, and the inventory says
        // so rather than silently reporting shell-deep truth as the whole truth.
        for account in v["accounts"].as_array().unwrap() {
            assert!(account["unsupported"].is_object());
            assert_eq!(
                account["unsupported"]["deny_network"],
                "no_network_logon_concept"
            );
            assert_eq!(account["builtin_guest"], false);
        }
    }

    /// The Linux payload the contract promises is one this collector can build.
    ///
    /// `docs/fixtures/telemetry_snapshot_linux.json` is otherwise only
    /// syntax-checked by the round-trip suite; this asserts the shaping code
    /// actually produces it, which is what will catch drift.
    #[test]
    fn the_linux_fixture_is_what_the_shaping_core_produces() {
        use serde_json::Value;

        let fixture: Value = serde_json::from_str(
            &std::fs::read_to_string(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../docs/fixtures/telemetry_snapshot_linux.json"),
            )
            .expect("read the Linux fixture"),
        )
        .expect("parse the Linux fixture");
        let expected = &fixture["snapshot"]["local_accounts"];

        let mut host = core::HostCaps::none();
        host.gap("deny_network", "no_network_logon_concept");
        host.gap("session_lock", "no_graphical_session");
        host.gap("session_warn", "no_user_notification_channel");

        let account = |name: &str,
                       display: &str,
                       enabled: bool,
                       is_admin: bool,
                       builtin_admin: bool,
                       password_last_set: Option<&str>,
                       last_logon: Option<&str>,
                       deny: &[&str],
                       extra: Vec<(&'static str, &'static str)>| {
            core::Account {
                name: name.to_string(),
                display: display.to_string(),
                kind: core::Kind::Local,
                deny_logon: deny.iter().map(|d| (*d).to_string()).collect(),
                enabled,
                is_admin,
                password_required: true,
                password_last_set: password_last_set.map(str::to_string),
                last_logon: last_logon.map(str::to_string),
                builtin_admin,
                builtin_guest: false,
                extra_unsupported: extra,
            }
        };

        let (accounts, admins, count) = core::shape(
            vec![
                account(
                    "papa",
                    "Papa",
                    true,
                    true,
                    false,
                    Some("2026-01-15T00:00:00Z"),
                    Some("2026-07-31T07:02:00Z"),
                    &[],
                    Vec::new(),
                ),
                account(
                    "kid",
                    "Kid",
                    true,
                    false,
                    false,
                    Some("2026-02-20T00:00:00Z"),
                    Some("2026-07-30T18:40:00Z"),
                    &["remote_interactive"],
                    Vec::new(),
                ),
                account(
                    "root",
                    "root",
                    true,
                    true,
                    true,
                    Some("2025-11-02T00:00:00Z"),
                    None,
                    &[],
                    vec![
                        ("set_admin", "root_account"),
                        ("set_enabled", "root_account"),
                    ],
                ),
                account(
                    "svc-backup",
                    "Backup service",
                    false,
                    false,
                    false,
                    None,
                    None,
                    &[],
                    vec![("set_enabled", "nologin_shell")],
                ),
            ],
            &host,
        );

        assert_eq!(Value::from(accounts), expected["accounts"]);
        assert_eq!(Value::from(admins), expected["admins"]);
        assert_eq!(Value::from(count), expected["count"]);
        assert_eq!(
            core::password_policy_with_gaps(
                Some(12),
                Some(90),
                None,
                &[("lockout_threshold", "pam_faillock_not_enabled")],
            ),
            expected["password_policy"]
        );
    }

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["accounts"].as_array().unwrap().len(), 0);
        assert_eq!(v["admins"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
    }
}
