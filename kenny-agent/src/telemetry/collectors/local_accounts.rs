//! `local_accounts` section — local users plus Administrators-group membership.
//!
//! Real data from `Get-LocalUser` on Windows; admin membership comes from
//! `Get-LocalGroupMember -SID S-1-5-32-544` (the well-known Administrators group
//! SID, locale-proof) and is matched to users by SID. Built-ins are marked via the
//! well-known RID suffixes (`-500` Administrator, `-501` Guest).
//!
//! **Privacy/minimality:** full SIDs never go on the wire — all SID matching
//! happens inside the probe, and only booleans leave it (ADR-0026 stance,
//! docs/protocol.md v0.10).

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `local_accounts` section.
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
            json!({ "accounts": [], "admins": [], "count": 0 }),
        )
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    /// One local account, as read from the probe (SID already reduced to booleans).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Account {
        pub name: String,
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
            Some(Account {
                name: row.get("name")?.as_str()?.to_string(),
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
                json!({
                    "name": a.name,
                    "enabled": a.enabled,
                    "is_admin": a.is_admin,
                    "password_required": a.password_required,
                    "password_last_set": a.password_last_set,
                    "last_logon": a.last_logon,
                    "builtin_admin": a.builtin_admin,
                    "builtin_guest": a.builtin_guest,
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
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-LocalUser` joined by SID with the Administrators group. The SIDs are
    /// compared inside the probe and reduced to booleans — they never leave it.
    pub fn collect() -> Section {
        let script = r#"
$adminSids = @()
try {
  # Well-known Administrators group SID; -SID is locale-proof. Get-LocalGroupMember
  # can throw on orphaned SIDs, hence best-effort.
  $adminSids = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
    ForEach-Object { [string]$_.SID.Value })
} catch {}
$out = @()
Get-LocalUser -ErrorAction SilentlyContinue | ForEach-Object {
  $sid = [string]$_.SID.Value
  $out += [pscustomobject]@{
    name = [string]$_.Name
    enabled = [bool]$_.Enabled
    is_admin = ($adminSids -contains $sid)
    password_required = [bool]$_.PasswordRequired
    last_logon = if ($_.LastLogon) { (Get-Date $_.LastLogon).ToUniversalTime().ToString('o') } else { $null }
    password_last_set = if ($_.PasswordLastSet) { (Get-Date $_.PasswordLastSet).ToUniversalTime().ToString('o') } else { $null }
    builtin_admin = $sid.EndsWith('-500')
    builtin_guest = $sid.EndsWith('-501')
  }
}
ConvertTo-Json -Compress @($out)
"#;

        let rows = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();
        let accounts: Vec<core::Account> =
            rows.iter().filter_map(core::Account::from_row).collect();
        let (accounts, admins, count) = core::shape(accounts);

        let n_admins = admins.len();
        let plural = if n_admins == 1 { "" } else { "s" };
        Section::with_fields(
            Status::Ok,
            format!("{count} accounts, {n_admins} admin{plural}"),
            json!({ "accounts": accounts, "admins": admins, "count": count }),
        )
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

    #[cfg(not(windows))]
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
