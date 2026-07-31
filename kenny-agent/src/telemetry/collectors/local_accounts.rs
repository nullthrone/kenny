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

    /// LSA logon rights kenny can deny, in stable wire order.
    ///
    /// `SeDenyInteractiveLogonRight` is deliberately absent: it can lock out the
    /// sole console user and kenny has no remote console to recover with (ADR-0046).
    pub const DENY_RIGHTS: [&str; 2] = ["network", "remote_interactive"];

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
        json!({
            "applies_to": "local_only",
            "min_length": min_length,
            "max_age_days": max_age_days,
            "lockout_threshold": lockout_threshold,
        })
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

    /// Sort accounts by name and derive the `admins` list (enabled *and* disabled
    /// admin names, sorted). Returns `(accounts, admins, count)`.
    pub fn shape(mut accounts: Vec<Account>) -> (Vec<Value>, Vec<String>, usize) {
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
                let unsupported: serde_json::Map<String, Value> = a
                    .kind
                    .unsupported()
                    .into_iter()
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
            let (out, admins, count) = shape(vec![
                account("papa", true, true),
                account("kid", true, false),
                disabled_admin,
            ]);
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
            let (out, _, _) = shape(vec![msa, account("papa", true, true)]);

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
        let (accounts, admins, count) = core::shape(accounts);

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
mod linux_impl {
    use super::*;
    use std::collections::HashSet;

    /// Shells that mean "no interactive login" — such accounts report `enabled=false`.
    const NOLOGIN_SHELLS: &[&str] = &[
        "/usr/sbin/nologin",
        "/sbin/nologin",
        "/bin/false",
        "/usr/bin/false",
    ];

    /// Groups whose members hold administrative (root-equivalent) rights.
    const ADMIN_GROUPS: &[&str] = &["sudo", "wheel", "admin"];

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

    /// Parse `/etc/passwd` + `/etc/group` into accounts.
    ///
    /// Keeps root (uid 0) and human accounts (uid >= 1000), skipping the `nobody`
    /// placeholder (uid 65534). `enabled` reflects a real login shell; `is_admin`
    /// is root or membership in a sudo/wheel/admin group. `builtin_admin` and
    /// `builtin_guest` are always `false` on Linux — those fields encode
    /// Windows RID-500/501 semantics that have no Linux analogue. The shadow file
    /// is root-only and never read, so `password_required` defaults to `true`.
    fn parse_accounts(passwd: &str, group: &str) -> Vec<core::Account> {
        let admins = admin_members(group);
        passwd
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
                if uid == 65534 || !(uid == 0 || uid >= 1000) {
                    return None;
                }
                let enabled = !shell.is_empty() && !NOLOGIN_SHELLS.contains(&shell);
                let is_admin = uid == 0 || admins.contains(name);
                Some(core::Account {
                    name: name.to_string(),
                    // Linux has no PrincipalSource; every /etc/passwd entry is local
                    // by construction, and GECOS supplies the display label.
                    display: core::display_label(gecos.split(',').next(), name),
                    kind: core::Kind::Local,
                    deny_logon: Vec::new(),
                    enabled,
                    is_admin,
                    password_required: true,
                    password_last_set: None,
                    last_logon: None,
                    builtin_admin: false,
                    builtin_guest: false,
                })
            })
            .collect()
    }

    /// Read `/etc/passwd` + `/etc/group` and shape the section.
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
        let accounts = parse_accounts(&passwd, &group);
        let (accounts, admins, count) = core::shape(accounts);

        let n_admins = admins.len();
        let plural = if n_admins == 1 { "" } else { "s" };
        Section::with_fields(
            Status::Ok,
            format!("{count} accounts, {n_admins} admin{plural}"),
            json!({
                "accounts": accounts, "admins": admins, "count": count,
                // PAM/login.defs have no single machine-wide equivalent worth
                // guessing at, and the governance tools are Windows-only anyway.
                "password_policy": core::password_policy(None, None, None),
            }),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn parse_accounts_selects_users_and_admins() {
            let passwd = "\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
papa:x:1000:1000:Papa:/home/papa:/bin/bash
kid:x:1001:1001:Kid:/home/kid:/bin/bash
svc:x:1002:1002:Service:/home/svc:/usr/sbin/nologin
";
            let group = "\
root:x:0:
sudo:x:27:papa
wheel:x:998:
";
            let accounts = parse_accounts(passwd, group);
            let names: Vec<&str> = accounts.iter().map(|a| a.name.as_str()).collect();
            // System users (daemon uid 1) and nobody (65534) are excluded.
            assert_eq!(names, vec!["root", "papa", "kid", "svc"]);

            let root = &accounts[0];
            assert!(root.is_admin, "uid 0 is admin");
            assert!(root.enabled);
            assert!(!root.builtin_admin, "no Windows RID-500 analogue on Linux");
            assert!(!root.builtin_guest);
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
