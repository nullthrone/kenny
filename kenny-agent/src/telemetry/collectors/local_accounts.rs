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
    #[cfg(target_os = "linux")]
    {
        linux_impl::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
                let _gecos = cols.next()?;
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
                json!({ "accounts": [], "admins": [], "count": 0 }),
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
            json!({ "accounts": accounts, "admins": admins, "count": count }),
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
