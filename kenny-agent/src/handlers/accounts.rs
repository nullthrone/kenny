//! `account_*` + `password_policy_set` — Windows account governance (ADR-0046).
//!
//! One tool family for **local and Microsoft accounts alike**. That is not an
//! abstraction the agent maintains: a Microsoft account on a workgroup PC *is* a SAM
//! entry with a machine-local SID and profile, so `Enable-LocalUser`,
//! `Remove-LocalGroupMember` and the LSA account rights operate on it exactly as they
//! do on a local account. The one genuine asymmetry — an MSA can only be *added* to a
//! PC interactively — is reported per account in the `local_accounts` telemetry
//! section's `unsupported` map, not branched on here.
//!
//! `principal` is the SAM account name. Full SIDs stay inside the probes; nothing
//! here puts a SID or a Microsoft-account address on the wire.
//!
//! **Self-protection** ([`guard`]) is not remotely overridable and refuses with
//! `blocked`: a governance call must never be able to lock everyone out of the
//! machine. It is deliberately `#[cfg]`-free and runs *before* the platform split, so
//! Linux CI exercises it — the same discipline `webfilter::validate_domains` uses.

#[cfg_attr(not(windows), allow(unused_imports))]
use serde_json::{json, Value};

use crate::protocol::ErrorCode;
use crate::telemetry::collectors::local_accounts::core::DENY_RIGHTS;

// The guard and its `Action` are consumed by the Windows impl and by the portable
// unit tests that run on every platform. A non-test Linux `cargo build` has neither
// consumer, so silence the dead-code warnings there — the same pattern
// `webfilter.rs` uses for its hosts-splicing core. The guard itself stays
// `#[cfg]`-free on purpose: it is the safety net, and it must be testable on CI.

/// Upper bound on the pre-action warning shown to the signed-in user, in seconds.
///
/// The handler sleeps for this long before acting, so it is bounded well under the
/// server's per-call timeout — a warning is a courtesy, not a scheduler. Real
/// time-based enforcement is deferred to its own ADR.
const MAX_WARN_SECONDS: u64 = 60;

/// What a governance call wants to do to an existing account.
///
/// Only the variants the self-protection guard needs to distinguish; the handlers
/// carry their own arguments.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Disable,
    Demote,
    DenyLogon,
    Delete,
    /// Lock or log off a live session — never guarded, see [`guard`].
    Session,
}

#[cfg_attr(not(windows), allow(dead_code))]
impl Action {
    fn verb(self) -> &'static str {
        match self {
            Action::Disable => "disable",
            Action::Demote => "remove administrator rights from",
            Action::DenyLogon => "deny logon rights to",
            Action::Delete => "delete",
            Action::Session => "act on the session of",
        }
    }
}

/// Refuse governance calls that would leave the machine unadministrable.
///
/// `inventory` is the `accounts` array of the `local_accounts` section, so the guard
/// judges exactly the state the operator saw.
///
/// Two invariants:
///
/// 1. **The last enabled administrator is untouchable.** Disabling, demoting,
///    deleting or denying logon to the only remaining enabled admin locks every
///    human out of the PC, and kenny has no console to recover with. This is the
///    account-governance twin of `webfilter`'s reserved-name rule.
/// 2. **Built-in accounts are not deletable.** RID 500/501 cannot be removed;
///    Windows would refuse anyway, and failing here gives a clear reason instead of
///    an opaque cmdlet error.
///
/// Session actions are never guarded: locking or logging off an administrator is
/// reversible by signing back in, so it cannot lock anyone out.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn guard(
    action: Action,
    principal: &str,
    inventory: &[Value],
) -> Result<(), (ErrorCode, String)> {
    let find = |name: &str| {
        inventory.iter().find(|a| {
            a.get("name")
                .and_then(Value::as_str)
                .is_some_and(|n| n.eq_ignore_ascii_case(name))
        })
    };
    let Some(target) = find(principal) else {
        return Err((
            ErrorCode::NotFound,
            format!("no account named {principal:?} on this machine"),
        ));
    };
    if action == Action::Session {
        return Ok(());
    }

    let flag = |a: &Value, key: &str| a.get(key).and_then(Value::as_bool).unwrap_or(false);

    if action == Action::Delete && (flag(target, "builtin_admin") || flag(target, "builtin_guest"))
    {
        return Err((
            ErrorCode::Blocked,
            format!("{principal:?} is a built-in Windows account and cannot be deleted"),
        ));
    }

    // Only an *enabled admin* can be the last one standing. An already-disabled or
    // non-admin account is free to change however the operator likes.
    if !(flag(target, "enabled") && flag(target, "is_admin")) {
        return Ok(());
    }
    let other_admins = inventory
        .iter()
        .filter(|a| {
            flag(a, "enabled")
                && flag(a, "is_admin")
                && a.get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|n| !n.eq_ignore_ascii_case(principal))
        })
        .count();
    if other_admins == 0 {
        return Err((
            ErrorCode::Blocked,
            format!(
                "refusing to {} {principal:?}: it is the last enabled administrator, \
                 and doing so would lock everyone out of this machine",
                action.verb()
            ),
        ));
    }
    Ok(())
}

/// Validate a `deny` set for `account_set_logon_rights`.
///
/// The set is absolute (it replaces whatever is configured), so `[]` clears both.
/// `interactive` is rejected by name rather than silently ignored: an operator who
/// asks for it is trying to do something kenny deliberately will not do, and a quiet
/// no-op would look like it worked.
pub fn validate_deny_rights(deny: &[String]) -> Result<Vec<String>, (ErrorCode, String)> {
    let mut out: Vec<String> = Vec::new();
    for raw in deny {
        let right = raw.trim().to_lowercase();
        if right == "interactive" {
            return Err((
                ErrorCode::BadArgs,
                "denying interactive logon is not supported: it can lock out the only \
                 console user and kenny has no remote console to recover with"
                    .to_string(),
            ));
        }
        if !DENY_RIGHTS.contains(&right.as_str()) {
            return Err((
                ErrorCode::BadArgs,
                format!("unknown logon right {raw:?}; expected any of {DENY_RIGHTS:?}"),
            ));
        }
        if !out.contains(&right) {
            out.push(right);
        }
    }
    // Stable wire order regardless of how the caller listed them.
    out.sort_by_key(|r| {
        DENY_RIGHTS
            .iter()
            .position(|d| d == r)
            .unwrap_or(usize::MAX)
    });
    Ok(out)
}

/// Validate `password_policy_set` arguments, returning the fields actually set.
///
/// Every field is optional and an omitted one is left untouched — a policy call that
/// silently reset the fields it was not asked about would be a footgun. An empty call
/// is rejected rather than treated as a no-op, since it is always a mistake.
pub fn validate_password_policy(
    args: &Value,
) -> Result<Vec<(&'static str, u32)>, (ErrorCode, String)> {
    // (wire key, inclusive max). Bounds mirror what Windows itself accepts, so an
    // out-of-range value fails here with a readable reason instead of inside secedit.
    const FIELDS: [(&str, u32); 3] = [
        ("min_length", 128),
        ("max_age_days", 999),
        ("lockout_threshold", 999),
    ];
    let mut out = Vec::new();
    for (key, max) in FIELDS {
        let Some(value) = args.get(key) else { continue };
        if value.is_null() {
            continue;
        }
        let n = value
            .as_u64()
            .filter(|n| *n <= u64::from(max))
            .ok_or_else(|| {
                (
                    ErrorCode::BadArgs,
                    format!("{key} must be an integer between 0 and {max}"),
                )
            })?;
        out.push((
            FIELDS
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(k, _)| *k)
                .unwrap(),
            n as u32,
        ));
    }
    if out.is_empty() {
        return Err((
            ErrorCode::BadArgs,
            "password_policy_set needs at least one of min_length, max_age_days, \
             lockout_threshold"
                .to_string(),
        ));
    }
    Ok(out)
}

/// Read `principal` from the args.
fn principal_arg(args: &Value) -> Result<String, (ErrorCode, String)> {
    let raw = args
        .get("principal")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    if raw.is_empty() {
        return Err((ErrorCode::BadArgs, "principal is required".to_string()));
    }
    Ok(raw.to_string())
}

fn bool_arg(args: &Value, key: &str) -> Result<bool, (ErrorCode, String)> {
    args.get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| (ErrorCode::BadArgs, format!("{key} must be a boolean")))
}

#[cfg_attr(windows, allow(dead_code))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

/// `account_set_enabled` — MUTATING. Suspend or restore an account, either kind.
pub async fn set_enabled(args: Value) -> Result<Value, (ErrorCode, String)> {
    let principal = principal_arg(&args)?;
    let enabled = bool_arg(&args, "enabled")?;
    #[cfg(windows)]
    {
        if !enabled {
            guard(Action::Disable, &principal, &windows_impl::inventory())?;
        }
        windows_impl::set_enabled(&principal, enabled)
    }
    #[cfg(not(windows))]
    {
        let _ = (principal, enabled);
        Err(unsupported("account_set_enabled"))
    }
}

/// `account_set_admin` — MUTATING. The strongest lever kenny has: without local
/// administrator rights, every other control it applies actually holds.
pub async fn set_admin(args: Value) -> Result<Value, (ErrorCode, String)> {
    let principal = principal_arg(&args)?;
    let admin = bool_arg(&args, "admin")?;
    #[cfg(windows)]
    {
        if !admin {
            guard(Action::Demote, &principal, &windows_impl::inventory())?;
        }
        windows_impl::set_admin(&principal, admin)
    }
    #[cfg(not(windows))]
    {
        let _ = (principal, admin);
        Err(unsupported("account_set_admin"))
    }
}

/// `account_set_logon_rights` — MUTATING. `deny` replaces the configured set.
pub async fn set_logon_rights(args: Value) -> Result<Value, (ErrorCode, String)> {
    let principal = principal_arg(&args)?;
    let requested: Vec<String> = args
        .get("deny")
        .and_then(Value::as_array)
        .ok_or_else(|| (ErrorCode::BadArgs, "deny must be an array".to_string()))?
        .iter()
        .map(|v| v.as_str().unwrap_or_default().to_string())
        .collect();
    let deny = validate_deny_rights(&requested)?;
    #[cfg(windows)]
    {
        if !deny.is_empty() {
            guard(Action::DenyLogon, &principal, &windows_impl::inventory())?;
        }
        windows_impl::set_logon_rights(&principal, &deny)
    }
    #[cfg(not(windows))]
    {
        let _ = (principal, deny);
        Err(unsupported("account_set_logon_rights"))
    }
}

/// `account_create` — MUTATING. **Local accounts only** — the one asymmetric verb.
pub async fn create(args: Value) -> Result<Value, (ErrorCode, String)> {
    let name = args
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|n| !n.is_empty())
        .ok_or_else(|| (ErrorCode::BadArgs, "name is required".to_string()))?
        .to_string();
    let password = args
        .get("password")
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty())
        .ok_or_else(|| (ErrorCode::BadArgs, "password is required".to_string()))?
        .to_string();
    let display_name = args
        .get("display_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let admin = args.get("admin").and_then(Value::as_bool).unwrap_or(false);
    #[cfg(windows)]
    {
        windows_impl::create(&name, &password, &display_name, admin)
    }
    #[cfg(not(windows))]
    {
        let _ = (name, password, display_name, admin);
        Err(unsupported("account_create"))
    }
}

/// `account_delete` — MUTATING. For a Microsoft-account-backed entry this unlinks
/// the account from this PC; the cloud account itself is untouched.
pub async fn delete(args: Value) -> Result<Value, (ErrorCode, String)> {
    let principal = principal_arg(&args)?;
    let remove_profile = args
        .get("remove_profile")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    #[cfg(windows)]
    {
        guard(Action::Delete, &principal, &windows_impl::inventory())?;
        windows_impl::delete(&principal, remove_profile)
    }
    #[cfg(not(windows))]
    {
        let _ = (principal, remove_profile);
        Err(unsupported("account_delete"))
    }
}

/// `account_session_action` — MUTATING. Lock or log off an account's live session(s).
///
/// Operator-triggered only. Automatic, schedule-driven enforcement is deliberately
/// out of scope here (ADR-0046): it needs a timer inside the agent, which breaks the
/// stateless-collector invariant of ADR-0007 and deserves its own decision.
pub async fn session_action(args: Value) -> Result<Value, (ErrorCode, String)> {
    let principal = principal_arg(&args)?;
    let action = args
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if action != "lock" && action != "logoff" {
        return Err((
            ErrorCode::BadArgs,
            format!("action must be \"lock\" or \"logoff\", got {action:?}"),
        ));
    }
    let warn_seconds = args
        .get("warn_seconds")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if warn_seconds > MAX_WARN_SECONDS {
        return Err((
            ErrorCode::BadArgs,
            format!("warn_seconds must be between 0 and {MAX_WARN_SECONDS}"),
        ));
    }
    #[cfg(windows)]
    {
        guard(Action::Session, &principal, &windows_impl::inventory())?;
        windows_impl::session_action(&principal, &action, warn_seconds).await
    }
    #[cfg(not(windows))]
    {
        let _ = (principal, action, warn_seconds);
        Err(unsupported("account_session_action"))
    }
}

/// `password_policy_set` — MUTATING, machine-wide.
///
/// The result always carries `applies_to: "local_only"`: a Microsoft account's
/// password lives under Microsoft's cloud policy and ignores this entirely. A
/// consumer must be able to say so without having read the ADR.
pub async fn password_policy_set(args: Value) -> Result<Value, (ErrorCode, String)> {
    let fields = validate_password_policy(&args)?;
    #[cfg(windows)]
    {
        windows_impl::password_policy_set(&fields)
    }
    #[cfg(not(windows))]
    {
        let _ = fields;
        Err(unsupported("password_policy_set"))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::{local_accounts, winps};

    /// Current account inventory for the self-protection guard.
    ///
    /// Deliberately re-runs the telemetry collector rather than keeping cached state:
    /// the guard must judge the machine as it is *now*, and this way it can never
    /// disagree with what the dashboard showed.
    pub fn inventory() -> Vec<Value> {
        local_accounts::collect()
            .into_value()
            .get("accounts")
            .and_then(|a| a.as_array().cloned())
            .unwrap_or_default()
    }

    /// Escape a string for embedding in a single-quoted PowerShell literal.
    fn q(s: &str) -> String {
        s.replace('\'', "''")
    }

    /// Run a script that must print `OK` on success, or a message to fail with.
    ///
    /// PowerShell's exit code is unreliable across cmdlet error modes, so success is
    /// signalled in stdout instead — the same "inspect the output, don't trust the
    /// exit code" stance `winps::run_command_output` documents.
    fn run_marked(script: &str, tool: &str) -> Result<(), (ErrorCode, String)> {
        let wrapped = format!("$ErrorActionPreference='Stop'\ntry {{\n{script}\nWrite-Output 'OK'\n}} catch {{ Write-Output ('ERR: ' + $_.Exception.Message) }}");
        let out = winps::run_text(&wrapped).unwrap_or_default();
        let out = out.trim();
        if out.lines().any(|l| l.trim() == "OK") {
            return Ok(());
        }
        let detail = out
            .lines()
            .find_map(|l| l.trim().strip_prefix("ERR: "))
            .unwrap_or("the probe produced no result")
            .to_string();
        Err((ErrorCode::ExecFailed, format!("{tool} failed: {detail}")))
    }

    /// The account's `kind`, echoed in results so a caller can see which sort of
    /// account it just changed without a second round trip.
    fn kind_of(principal: &str) -> String {
        inventory()
            .iter()
            .find(|a| {
                a.get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|n| n.eq_ignore_ascii_case(principal))
            })
            .and_then(|a| a.get("kind").and_then(Value::as_str))
            .unwrap_or("unknown")
            .to_string()
    }

    pub fn set_enabled(principal: &str, enabled: bool) -> Result<Value, (ErrorCode, String)> {
        let cmdlet = if enabled {
            "Enable-LocalUser"
        } else {
            "Disable-LocalUser"
        };
        run_marked(
            &format!("{cmdlet} -Name '{}'", q(principal)),
            "account_set_enabled",
        )?;
        Ok(json!({
            "ok": true, "principal": principal, "kind": kind_of(principal),
            "enabled": enabled,
        }))
    }

    pub fn set_admin(principal: &str, admin: bool) -> Result<Value, (ErrorCode, String)> {
        // -SID for the group is locale-proof (Administrators is translated on
        // non-English Windows); the member is the SAM name, which is not.
        let cmdlet = if admin {
            "Add-LocalGroupMember"
        } else {
            "Remove-LocalGroupMember"
        };
        run_marked(
            &format!("{cmdlet} -SID 'S-1-5-32-544' -Member '{}'", q(principal)),
            "account_set_admin",
        )?;
        Ok(json!({
            "ok": true, "principal": principal, "kind": kind_of(principal),
            "admin": admin,
        }))
    }

    /// Apply the deny set via `secedit`.
    ///
    /// LSA account rights have no cmdlet. `secedit /configure` applies only the
    /// privileges named in the template and leaves every other right alone, so
    /// writing just these two lines is safe. The INF must be UTF-16 with the
    /// `$CHICAGO$` signature or secedit rejects it silently.
    pub fn set_logon_rights(
        principal: &str,
        deny: &[String],
    ) -> Result<Value, (ErrorCode, String)> {
        let network = if deny.iter().any(|d| d == "network") {
            "'*' + $sid"
        } else {
            "''"
        };
        let remote = if deny.iter().any(|d| d == "remote_interactive") {
            "'*' + $sid"
        } else {
            "''"
        };
        // Each right is written as the full desired membership for this principal.
        // Other principals already holding the right would be dropped, so preserve
        // them: read the current lists and swap only this SID in or out.
        let script = format!(
            r#"
$sid = (Get-LocalUser -Name '{name}').SID.Value
$stamp = [guid]::NewGuid().ToString('N')
$export = Join-Path $env:TEMP ("kenny-rights-$stamp.inf")
$apply  = Join-Path $env:TEMP ("kenny-rights-$stamp-apply.inf")
$db     = Join-Path $env:TEMP ("kenny-rights-$stamp.sdb")
$null = & secedit.exe /export /areas USER_RIGHTS /cfg $export /quiet
$cur = @{{ SeDenyNetworkLogonRight = @(); SeDenyRemoteInteractiveLogonRight = @() }}
if (Test-Path $export) {{
  foreach ($line in (Get-Content -LiteralPath $export -Encoding Unicode)) {{
    foreach ($k in @('SeDenyNetworkLogonRight','SeDenyRemoteInteractiveLogonRight')) {{
      if ($line -match ('^\s*' + $k + '\s*=\s*(.*)$')) {{
        $cur[$k] = @($matches[1] -split ',' | ForEach-Object {{ $_.Trim() }} | Where-Object {{ $_ }})
      }}
    }}
  }}
}}
$want = @{{ SeDenyNetworkLogonRight = {network}; SeDenyRemoteInteractiveLogonRight = {remote} }}
$lines = @('[Unicode]','Unicode=yes','[Version]','signature="$CHICAGO$"','Revision=1','[Privilege Rights]')
foreach ($k in @('SeDenyNetworkLogonRight','SeDenyRemoteInteractiveLogonRight')) {{
  $members = @($cur[$k] | Where-Object {{ $_.TrimStart('*') -ne $sid }})
  if ($want[$k]) {{ $members += $want[$k] }}
  $lines += ($k + ' = ' + (($members | Select-Object -Unique) -join ','))
}}
$lines | Out-File -LiteralPath $apply -Encoding Unicode -Force
$null = & secedit.exe /configure /db $db /cfg $apply /areas USER_RIGHTS /quiet
Remove-Item -LiteralPath $export,$apply,$db -Force -ErrorAction SilentlyContinue
"#,
            name = q(principal),
            network = network,
            remote = remote,
        );
        run_marked(&script, "account_set_logon_rights")?;
        Ok(json!({
            "ok": true, "principal": principal, "kind": kind_of(principal),
            "deny": deny,
        }))
    }

    pub fn create(
        name: &str,
        password: &str,
        display_name: &str,
        admin: bool,
    ) -> Result<Value, (ErrorCode, String)> {
        let full_name = if display_name.trim().is_empty() {
            String::new()
        } else {
            format!(" -FullName '{}'", q(display_name))
        };
        let promote = if admin {
            format!(
                "\nAdd-LocalGroupMember -SID 'S-1-5-32-544' -Member '{}'",
                q(name)
            )
        } else {
            String::new()
        };
        let script = format!(
            "$pw = ConvertTo-SecureString '{pw}' -AsPlainText -Force\n\
             New-LocalUser -Name '{name}' -Password $pw{full_name} -AccountNeverExpires | Out-Null{promote}",
            pw = q(password),
            name = q(name),
        );
        run_marked(&script, "account_create")?;
        // Always `local`: a Microsoft account cannot be created from here at all,
        // which is exactly the asymmetry the inventory advertises.
        Ok(json!({ "ok": true, "principal": name, "kind": "local" }))
    }

    pub fn delete(principal: &str, remove_profile: bool) -> Result<Value, (ErrorCode, String)> {
        let profile = if remove_profile {
            // Resolve the profile by SID, not by path: a profile directory is not
            // reliably named after the account (least of all for a Microsoft
            // account, whose folder is a truncated form of the address).
            "\n$p = Get-CimInstance Win32_UserProfile -Filter \"SID='$sid'\" -ErrorAction SilentlyContinue\nif ($p) { Remove-CimInstance -InputObject $p }"
        } else {
            ""
        };
        let script = format!(
            "$sid = (Get-LocalUser -Name '{name}').SID.Value\n\
             Remove-LocalUser -Name '{name}'{profile}",
            name = q(principal),
        );
        run_marked(&script, "account_delete")?;
        Ok(json!({
            "ok": true, "principal": principal, "profile_removed": remove_profile,
        }))
    }

    /// Lock or log off every interactive session belonging to `principal`.
    ///
    /// `lock` is implemented as `WTSDisconnectSession`, not `LockWorkStation`: a
    /// session-0 service cannot lock another session directly, and disconnecting the
    /// console session returns it to the sign-in screen with the session and its apps
    /// intact — which is what "lock" means for this purpose. Documented rather than
    /// glossed, because the two are not identical: a disconnect also drops any RDP
    /// connection to that session.
    ///
    /// An account with no live session succeeds with an empty `sessions` list; there
    /// is nothing to do and nothing has gone wrong.
    pub async fn session_action(
        principal: &str,
        action: &str,
        warn_seconds: u64,
    ) -> Result<Value, (ErrorCode, String)> {
        let sessions = wts::sessions_for(principal);
        if sessions.is_empty() {
            return Ok(json!({
                "ok": true, "principal": principal, "action": action, "sessions": [],
            }));
        }

        if warn_seconds > 0 {
            let verb = if action == "lock" {
                "locked"
            } else {
                "signed out"
            };
            let body =
                format!("This PC will be {verb} in {warn_seconds} seconds. Please save your work.");
            for (session_id, _) in &sessions {
                wts::notify(*session_id, "kenny", &body);
            }
            // Non-blocking notify plus an async sleep: the message box lives in the
            // user's session while the agent stays responsive to the tunnel.
            tokio::time::sleep(std::time::Duration::from_secs(warn_seconds)).await;
        }

        let acted: Vec<Value> = sessions
            .iter()
            .map(|(session_id, state)| {
                let ok = if action == "lock" {
                    wts::disconnect(*session_id)
                } else {
                    wts::logoff(*session_id)
                };
                json!({ "session_id": session_id, "state": state, "acted": ok })
            })
            .collect();

        Ok(json!({
            "ok": acted.iter().all(|s| s["acted"] == true),
            "principal": principal, "action": action, "sessions": acted,
        }))
    }

    pub fn password_policy_set(
        fields: &[(&'static str, u32)],
    ) -> Result<Value, (ErrorCode, String)> {
        // `net accounts` takes each setting independently, so omitted fields stay
        // untouched without having to round-trip the whole policy through secedit.
        let mut commands = Vec::new();
        let mut applied = serde_json::Map::new();
        for (key, value) in fields {
            let flag = match *key {
                "min_length" => "/minpwlen",
                "max_age_days" => "/maxpwage",
                "lockout_threshold" => "/lockoutthreshold",
                _ => continue,
            };
            // 0 means "never expires" for the age, which `net accounts` spells
            // `unlimited`; it rejects a literal 0 there.
            let arg = if flag == "/maxpwage" && *value == 0 {
                "unlimited".to_string()
            } else {
                value.to_string()
            };
            commands.push(format!(
                "$r = & net.exe accounts {flag}:{arg} 2>&1\nif ($LASTEXITCODE -ne 0) {{ throw ([string]$r) }}"
            ));
            applied.insert((*key).to_string(), Value::from(*value));
        }
        run_marked(&commands.join("\n"), "password_policy_set")?;
        Ok(json!({
            "ok": true,
            "applies_to": "local_only",
            "policy": Value::Object(applied),
        }))
    }

    /// Thin `windows-rs` wrappers over the Terminal Services session APIs.
    ///
    /// These are the only type-agnostic *runtime* levers kenny has: a session knows
    /// nothing about whether the signed-in identity came from SAM or from Microsoft's
    /// cloud, so lock/log off work identically for both — which is what compensates
    /// for Family Safety having no API at all.
    mod wts {
        use windows::core::PWSTR;
        use windows::Win32::System::RemoteDesktop::{
            WTSDisconnectSession, WTSEnumerateSessionsW, WTSFreeMemory, WTSLogoffSession,
            WTSQuerySessionInformationW, WTSSendMessageW, WTSUserName, WTS_CONNECTSTATE_CLASS,
            WTS_CURRENT_SERVER_HANDLE, WTS_SESSION_INFOW,
        };
        use windows::Win32::UI::WindowsAndMessaging::{MB_OK, MESSAGEBOX_RESULT};

        fn wide(s: &str) -> Vec<u16> {
            s.encode_utf16().chain(std::iter::once(0)).collect()
        }

        fn state_token(state: WTS_CONNECTSTATE_CLASS) -> &'static str {
            match state.0 {
                0 => "active",
                1 => "connecting",
                2 => "connected",
                4 => "disconnected",
                _ => "other",
            }
        }

        /// Session id + state for every session whose signed-in user is `principal`.
        ///
        /// Session 0 is skipped: it is the service session, has no interactive user,
        /// and logging it off would take the agent down with it.
        pub fn sessions_for(principal: &str) -> Vec<(u32, &'static str)> {
            let mut info: *mut WTS_SESSION_INFOW = std::ptr::null_mut();
            let mut count: u32 = 0;
            let ok = unsafe {
                WTSEnumerateSessionsW(Some(WTS_CURRENT_SERVER_HANDLE), 0, 1, &mut info, &mut count)
            };
            if ok.is_err() || info.is_null() {
                return Vec::new();
            }
            let mut out = Vec::new();
            for i in 0..count as usize {
                let entry = unsafe { &*info.add(i) };
                if entry.SessionId == 0 {
                    continue;
                }
                if session_user(entry.SessionId)
                    .is_some_and(|user| user.eq_ignore_ascii_case(principal))
                {
                    out.push((entry.SessionId, state_token(entry.State)));
                }
            }
            unsafe { WTSFreeMemory(info as *mut _) };
            out
        }

        fn session_user(session_id: u32) -> Option<String> {
            let mut buf = PWSTR::null();
            let mut bytes: u32 = 0;
            let ok = unsafe {
                WTSQuerySessionInformationW(
                    Some(WTS_CURRENT_SERVER_HANDLE),
                    session_id,
                    WTSUserName,
                    &mut buf,
                    &mut bytes,
                )
            };
            if ok.is_err() || buf.is_null() {
                return None;
            }
            let user = unsafe { buf.to_string() }.ok().filter(|u| !u.is_empty());
            unsafe { WTSFreeMemory(buf.as_ptr() as *mut _) };
            user
        }

        /// Show a message box in the target session. Fire-and-forget: `bWait = FALSE`
        /// so the agent is never blocked by a box nobody dismisses.
        pub fn notify(session_id: u32, title: &str, body: &str) {
            let (mut t, mut b) = (wide(title), wide(body));
            let mut response = MESSAGEBOX_RESULT(0);
            unsafe {
                let _ = WTSSendMessageW(
                    Some(WTS_CURRENT_SERVER_HANDLE),
                    session_id,
                    PWSTR(t.as_mut_ptr()),
                    (t.len() * 2) as u32,
                    PWSTR(b.as_mut_ptr()),
                    (b.len() * 2) as u32,
                    MB_OK,
                    0,
                    &mut response,
                    false,
                );
            }
        }

        /// `bwait = true`: block until the session has actually gone, so the tool's
        /// `acted` flag reports what happened rather than what was requested.
        pub fn disconnect(session_id: u32) -> bool {
            unsafe {
                WTSDisconnectSession(Some(WTS_CURRENT_SERVER_HANDLE), session_id, true).is_ok()
            }
        }

        pub fn logoff(session_id: u32) -> bool {
            unsafe { WTSLogoffSession(Some(WTS_CURRENT_SERVER_HANDLE), session_id, true).is_ok() }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn account(name: &str, enabled: bool, is_admin: bool) -> Value {
        json!({
            "name": name, "display": name, "kind": "local",
            "enabled": enabled, "is_admin": is_admin,
            "builtin_admin": false, "builtin_guest": false, "deny_logon": [],
        })
    }

    fn household() -> Vec<Value> {
        vec![
            account("papa", true, true),
            account("mama", true, true),
            account("kid", true, false),
        ]
    }

    #[test]
    fn guard_rejects_an_unknown_principal() {
        let err = guard(Action::Disable, "ghost", &household()).unwrap_err();
        assert_eq!(err.0, ErrorCode::NotFound);
        assert!(err.1.contains("ghost"));
    }

    #[test]
    fn guard_matches_the_principal_case_insensitively() {
        // Windows account names are case-insensitive; a call spelled "Kid" must not
        // fall through to "no such account" and then be blocked for the wrong reason.
        assert!(guard(Action::Disable, "KID", &household()).is_ok());
    }

    #[test]
    fn guard_allows_touching_an_admin_while_another_remains() {
        for action in [
            Action::Disable,
            Action::Demote,
            Action::DenyLogon,
            Action::Delete,
        ] {
            assert!(
                guard(action, "papa", &household()).is_ok(),
                "{action:?} should be allowed while mama is still an enabled admin"
            );
        }
    }

    #[test]
    fn guard_protects_the_last_enabled_administrator() {
        let inventory = vec![
            account("papa", true, true),
            // Another admin exists but is disabled, so it cannot rescue anyone.
            account("old-admin", false, true),
            account("kid", true, false),
        ];
        for action in [
            Action::Disable,
            Action::Demote,
            Action::DenyLogon,
            Action::Delete,
        ] {
            let err = guard(action, "papa", &inventory).unwrap_err();
            assert_eq!(err.0, ErrorCode::Blocked, "{action:?} must be blocked");
            assert!(err.1.contains("last enabled administrator"));
        }
        // A session action stays allowed: signing back in undoes it, so it cannot
        // lock anyone out.
        assert!(guard(Action::Session, "papa", &inventory).is_ok());
        // And the non-admin is unaffected by the invariant.
        assert!(guard(Action::Disable, "kid", &inventory).is_ok());
    }

    #[test]
    fn guard_refuses_to_delete_builtin_accounts() {
        let mut builtin = account("Administrator", false, true);
        builtin["builtin_admin"] = json!(true);
        let mut guest = account("Guest", false, false);
        guest["builtin_guest"] = json!(true);
        let inventory = vec![account("papa", true, true), builtin, guest];

        let err = guard(Action::Delete, "Administrator", &inventory).unwrap_err();
        assert_eq!(err.0, ErrorCode::Blocked);
        assert!(err.1.contains("built-in"));
        assert!(guard(Action::Delete, "Guest", &inventory).is_err());
        // Built-ins can still be disabled — that is the recommended hardening.
        assert!(guard(Action::Disable, "Administrator", &inventory).is_ok());
    }

    #[test]
    fn deny_rights_are_validated_normalized_and_ordered() {
        assert_eq!(validate_deny_rights(&[]).unwrap(), Vec::<String>::new());
        assert_eq!(
            validate_deny_rights(&["remote_interactive".into(), "NETWORK".into()]).unwrap(),
            vec!["network".to_string(), "remote_interactive".to_string()],
        );
        // Duplicates collapse.
        assert_eq!(
            validate_deny_rights(&["network".into(), "network".into()]).unwrap(),
            vec!["network".to_string()],
        );
    }

    #[test]
    fn denying_interactive_logon_is_refused_by_name() {
        // A silent no-op would look like it worked, which is worse than refusing.
        let err = validate_deny_rights(&["interactive".into()]).unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
        assert!(err.1.contains("console"));

        let err = validate_deny_rights(&["service".into()]).unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[test]
    fn password_policy_accepts_partial_updates_and_rejects_nonsense() {
        assert_eq!(
            validate_password_policy(&json!({ "min_length": 8 })).unwrap(),
            vec![("min_length", 8)],
        );
        // Omitted fields are simply not applied — they must not be reset to 0.
        let all = validate_password_policy(&json!({
            "min_length": 10, "max_age_days": 90, "lockout_threshold": 5
        }))
        .unwrap();
        assert_eq!(all.len(), 3);
        // An explicit null is the same as omitting.
        assert_eq!(
            validate_password_policy(&json!({ "min_length": 8, "max_age_days": null })).unwrap(),
            vec![("min_length", 8)],
        );

        for bad in [
            json!({}),
            json!({ "min_length": 500 }),
            json!({ "min_length": -1 }),
            json!({ "lockout_threshold": "many" }),
        ] {
            assert_eq!(
                validate_password_policy(&bad).unwrap_err().0,
                ErrorCode::BadArgs,
                "{bad} should be rejected"
            );
        }
    }

    #[tokio::test]
    async fn argument_errors_surface_before_any_platform_work() {
        // These must fail identically on Linux CI and on Windows: they are contract
        // violations, not platform gaps.
        assert_eq!(
            set_enabled(json!({ "enabled": true })).await.unwrap_err().0,
            ErrorCode::BadArgs
        );
        assert_eq!(
            set_enabled(json!({ "principal": "kid" }))
                .await
                .unwrap_err()
                .0,
            ErrorCode::BadArgs
        );
        assert_eq!(
            session_action(json!({ "principal": "kid", "action": "shutdown" }))
                .await
                .unwrap_err()
                .0,
            ErrorCode::BadArgs
        );
        assert_eq!(
            session_action(json!({
                "principal": "kid", "action": "lock", "warn_seconds": 6000
            }))
            .await
            .unwrap_err()
            .0,
            ErrorCode::BadArgs
        );
        assert_eq!(
            create(json!({ "name": "x" })).await.unwrap_err().0,
            ErrorCode::BadArgs
        );
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn off_windows_every_tool_is_unsupported() {
        let calls: Vec<Result<Value, (ErrorCode, String)>> = vec![
            set_enabled(json!({ "principal": "kid", "enabled": false })).await,
            set_admin(json!({ "principal": "kid", "admin": false })).await,
            set_logon_rights(json!({ "principal": "kid", "deny": ["network"] })).await,
            create(json!({ "name": "kid", "password": "pw" })).await,
            delete(json!({ "principal": "kid", "remove_profile": false })).await,
            session_action(json!({ "principal": "kid", "action": "lock" })).await,
            password_policy_set(json!({ "min_length": 8 })).await,
        ];
        for call in calls {
            let (code, message) = call.unwrap_err();
            assert_eq!(code, ErrorCode::Unsupported);
            assert!(message.contains("Windows"), "{message}");
        }
    }
}
