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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Disable,
    Demote,
    /// Deny one specific logon right — the capability verb is per right, because a
    /// host can support denying SSH while having no network-logon plane at all.
    DenyLogon(&'static str),
    Delete,
    /// Lock or log off a live session — never *guarded* for lockout, but still
    /// subject to the capability check, see [`guard`].
    Session(&'static str),
}

impl Action {
    fn verb(self) -> &'static str {
        match self {
            Action::Disable => "disable",
            Action::Demote => "remove administrator rights from",
            Action::DenyLogon(_) => "deny logon rights to",
            Action::Delete => "delete",
            Action::Session(_) => "act on the session of",
        }
    }

    /// The capability verb this action needs, as published in the inventory's
    /// per-account `unsupported` map (see docs/protocol.md § Capability negation).
    fn capability(self) -> &'static str {
        match self {
            Action::Disable => "set_enabled",
            Action::Demote => "set_admin",
            Action::DenyLogon("network") => "deny_network",
            Action::DenyLogon(_) => "deny_remote_interactive",
            Action::Delete => "delete",
            Action::Session("lock") => "session_lock",
            Action::Session(_) => "session_logoff",
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
/// 3. **The inventory's own capability map is enforced.** If the account publishes
///    this action's verb in its `unsupported` map, the call is refused with the
///    reason token. That is what keeps the capability set kenny *advertises* and the
///    one it *enforces* from drifting apart — they are the same list, read at call
///    time (ADR-0047). It is also how root, a shell-less account, a host without
///    `sshd` and a headless server are all protected without a single
///    platform-specific rule in this function.
///
/// Session actions are never guarded *for lockout*: locking or logging off an
/// administrator is reversible by signing back in, so it cannot lock anyone out.
/// They are still subject to rule 3.
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
    // Rule 3: refuse exactly what the inventory says this account cannot do. Runs
    // before the lockout rules so the more specific reason is the one reported.
    if let Some(reason) = target
        .get("unsupported")
        .and_then(Value::as_object)
        .and_then(|map| map.get(action.capability()))
        .and_then(Value::as_str)
    {
        return Err((
            ErrorCode::Blocked,
            format!(
                "cannot {} {principal:?} on this host: {} ({reason})",
                action.verb(),
                unsupported_reason(reason)
            ),
        ));
    }

    if matches!(action, Action::Session(_)) {
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

/// Expand a wire reason token into a sentence for the `blocked` message.
///
/// The wire carries stable tokens so consumers can localize; this is the agent's own
/// rendering for the one place a human reads the refusal directly — the tool result.
/// The dashboard has its own table and does not use this.
fn unsupported_reason(token: &str) -> &str {
    match token {
        "root_account" => "uid 0 is root, and group membership neither grants nor revokes that",
        "admin_via_sudoers" => {
            "its administrator rights come from a sudoers rule, which kenny never edits"
        }
        "nologin_shell" => "the account has no login shell, and kenny does not rewrite shells",
        "no_network_logon_concept" => "this OS has no per-account network sign-in to deny",
        "no_sshd" => "there is no SSH daemon, so there is no remote sign-in to deny",
        "sshd_no_include" => {
            "sshd_config has no Include line for its drop-in directory, and kenny will not add one"
        }
        "no_logind" => "there is no session manager to act through",
        "no_graphical_session" => "there is no graphical session to lock",
        "no_admin_group" => "this host has no sudo, wheel or admin group",
        "shadow_unreadable" => {
            "kenny cannot read /etc/shadow, so it is not running with the \
                                privileges this needs"
        }
        "password_in_cloud" => "the password lives in the account's cloud identity",
        "kind_unknown" => "kenny could not determine what kind of account this is",
        other => other,
    }
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

/// Map a validated logon right back to its `'static` form.
///
/// [`validate_deny_rights`] has already proved the string is one of [`DENY_RIGHTS`];
/// this only recovers the borrow so [`Action::DenyLogon`] can carry it.
fn static_right(right: &str) -> &'static str {
    DENY_RIGHTS
        .iter()
        .copied()
        .find(|d| *d == right)
        .unwrap_or("remote_interactive")
}

/// Validate a name for `account_create`.
///
/// Neither platform accepted arbitrary text before: Windows built a PowerShell
/// literal and relied on quoting, Linux would pass the name to `useradd`. Rejecting
/// the hostile shapes once, in the portable layer, means neither arm has to be the
/// last line of defence — and the rule is exercised on Linux CI regardless of which
/// arm is compiled.
///
/// The accepted set is deliberately narrower than either OS allows: a family fleet
/// has no need for account names that are also shell metacharacters.
pub fn validate_new_account_name(name: &str) -> Result<(), (ErrorCode, String)> {
    const MAX_LEN: usize = 32;
    if name.len() > MAX_LEN {
        return Err((
            ErrorCode::BadArgs,
            format!("name must be at most {MAX_LEN} characters"),
        ));
    }
    if name.starts_with('-') {
        // A leading dash turns the name into an option for every POSIX tool.
        return Err((
            ErrorCode::BadArgs,
            "name must not start with '-'".to_string(),
        ));
    }
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-')
    {
        return Err((
            ErrorCode::BadArgs,
            "name may contain only letters, digits, '.', '_' and '-'".to_string(),
        ));
    }
    Ok(())
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

#[cfg_attr(any(windows, target_os = "linux"), allow(dead_code))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is not available on this operating system"),
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
    #[cfg(target_os = "linux")]
    {
        // Guarded in both directions, unlike Windows: on Linux the inventory also
        // publishes *restore* restrictions (a shell-less account, an unreadable
        // /etc/shadow), and those must be refused rather than silently no-op.
        guard(Action::Disable, &principal, &linux_impl::inventory())?;
        linux_impl::set_enabled(&principal, enabled)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
    #[cfg(target_os = "linux")]
    {
        // Checked for promotion too: root and a sudoers-granted admin publish
        // `set_admin` as unsupported, and promoting them would report a change kenny
        // did not make.
        guard(Action::Demote, &principal, &linux_impl::inventory())?;
        linux_impl::set_admin(&principal, admin)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
        for right in &deny {
            guard(
                Action::DenyLogon(static_right(right)),
                &principal,
                &windows_impl::inventory(),
            )?;
        }
        windows_impl::set_logon_rights(&principal, &deny)
    }
    #[cfg(target_os = "linux")]
    {
        let inventory = linux_impl::inventory();
        for right in &deny {
            guard(
                Action::DenyLogon(static_right(right)),
                &principal,
                &inventory,
            )?;
        }
        linux_impl::set_logon_rights(&principal, &deny)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
    validate_new_account_name(&name)?;
    #[cfg(windows)]
    {
        windows_impl::create(&name, &password, &display_name, admin)
    }
    #[cfg(target_os = "linux")]
    {
        linux_impl::create(&name, &password, &display_name, admin)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
    #[cfg(target_os = "linux")]
    {
        guard(Action::Delete, &principal, &linux_impl::inventory())?;
        linux_impl::delete(&principal, remove_profile)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
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
    let session_verb = if action == "lock" { "lock" } else { "logoff" };
    #[cfg(windows)]
    {
        guard(
            Action::Session(session_verb),
            &principal,
            &windows_impl::inventory(),
        )?;
        windows_impl::session_action(&principal, &action, warn_seconds).await
    }
    #[cfg(target_os = "linux")]
    {
        guard(
            Action::Session(session_verb),
            &principal,
            &linux_impl::inventory(),
        )?;
        linux_impl::session_action(&principal, &action).await
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (principal, action, warn_seconds, session_verb);
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
    #[cfg(target_os = "linux")]
    {
        linux_impl::password_policy_set(&fields)
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = fields;
        Err(unsupported("password_policy_set"))
    }
}

/// Linux governance, on the `/etc/passwd` + `/etc/shadow` + `/etc/group` layer plus
/// `systemd-logind` and PAM (ADR-0047).
///
/// Two rules run through everything here:
///
/// 1. **Every command is an argv array, never a shell string.** Unlike the Windows
///    arm, which must build PowerShell text and quote into it, nothing on this side
///    is ever parsed by a shell — so an account name cannot become an injection.
/// 2. **The mechanism is separated from the execution.** Each verb has a pure
///    `plan_*` function returning the argv lists it intends to run, unit-tested on
///    CI without root, and a thin executor. The interesting half is therefore the
///    tested half.
///
/// kenny deliberately never writes `/etc/pam.d` or `/etc/sudoers`: a mistake in
/// either locks out authentication or sudo for the whole machine, and neither is
/// needed for anything in the tool catalog.
#[cfg(target_os = "linux")]
mod linux_impl {
    use std::process::Command;

    use super::*;
    use crate::telemetry::collectors::local_accounts::{self, linux_impl as inv};

    /// Current account inventory for the self-protection guard.
    ///
    /// Re-runs the telemetry collector rather than caching, exactly as the Windows
    /// arm does: the guard must judge the machine as it is *now*, and this way it
    /// can never disagree with what the dashboard showed.
    pub fn inventory() -> Vec<Value> {
        local_accounts::collect()
            .into_value()
            .get("accounts")
            .and_then(|a| a.as_array().cloned())
            .unwrap_or_default()
    }

    /// Run one argv, mapping a non-zero exit to `exec_failed` with the tool's own
    /// stderr — which is nearly always the actionable message ("user is currently
    /// used by process 1234").
    fn run(argv: &[String], tool: &str) -> Result<(), (ErrorCode, String)> {
        let (program, args) = argv
            .split_first()
            .ok_or_else(|| (ErrorCode::Internal, format!("{tool}: empty command")))?;
        let out = Command::new(program).args(args).output().map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("{tool}: could not run {program}: {e}"),
            )
        })?;
        if out.status.success() {
            return Ok(());
        }
        let stderr = String::from_utf8_lossy(&out.stderr);
        let stdout = String::from_utf8_lossy(&out.stdout);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        Err((
            ErrorCode::ExecFailed,
            format!("{tool}: {program} failed: {detail}"),
        ))
    }

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| (*s).to_string()).collect()
    }

    // ---- account_set_enabled -------------------------------------------------

    /// The commands that suspend or restore an account.
    ///
    /// Disabling sets an **account expiry** as well as locking the password. The
    /// password lock alone is not enough: `usermod -L` only prefixes the hash with
    /// `!`, and SSH public-key authentication never consults it. Expiry is evaluated
    /// in PAM's account stage, which sshd runs for every authentication method, so it
    /// is the only mechanism that closes all the doors (ADR-0047).
    pub fn plan_set_enabled(principal: &str, enabled: bool) -> Vec<Vec<String>> {
        if enabled {
            vec![
                argv(&["usermod", "--expiredate", "", principal]),
                argv(&["usermod", "-U", principal]),
            ]
        } else {
            vec![
                argv(&["usermod", "--expiredate", "1", principal]),
                argv(&["usermod", "-L", principal]),
            ]
        }
    }

    pub fn set_enabled(principal: &str, enabled: bool) -> Result<Value, (ErrorCode, String)> {
        let plan = plan_set_enabled(principal, enabled);
        // The expiry change is the one that must succeed; the password lock/unlock is
        // best-effort. `usermod -U` deliberately refuses to unlock an account whose
        // hash is a bare `!` — unlocking would leave it password-less — and turning
        // that refusal into an error would make a correct, conservative behaviour
        // look like a failure.
        run(&plan[0], "account_set_enabled")?;
        let _ = run(&plan[1], "account_set_enabled");
        Ok(json!({
            "ok": true, "principal": principal, "kind": "local",
            "enabled": enabled,
        }))
    }

    // ---- account_set_admin ---------------------------------------------------

    /// The commands that grant or revoke administrator rights.
    ///
    /// Promotion targets the *first admin group that exists on this host*; demotion
    /// strips **every** one the user is in. The asymmetry is deliberate: promoting
    /// into the "wrong" group is harmless because membership in any of them counts,
    /// while demoting from only one would leave the user an administrator and report
    /// success.
    ///
    /// `gpasswd` is used both ways because `usermod -aG` can only add — using it
    /// would force two different mechanisms for one verb.
    pub fn plan_set_admin(
        principal: &str,
        admin: bool,
        group_file: &str,
    ) -> Result<Vec<Vec<String>>, (ErrorCode, String)> {
        if admin {
            let group = inv::preferred_admin_group(group_file).ok_or_else(|| {
                (
                    ErrorCode::Unsupported,
                    "this host has no sudo, wheel or admin group to add the account to".to_string(),
                )
            })?;
            Ok(vec![argv(&["gpasswd", "--add", principal, group])])
        } else {
            Ok(inv::admin_groups_of(group_file, principal)
                .into_iter()
                .map(|group| argv(&["gpasswd", "--delete", principal, group]))
                .collect())
        }
    }

    pub fn set_admin(principal: &str, admin: bool) -> Result<Value, (ErrorCode, String)> {
        let group_file = std::fs::read_to_string("/etc/group").unwrap_or_default();
        for command in plan_set_admin(principal, admin, &group_file)? {
            run(&command, "account_set_admin")?;
        }
        Ok(json!({
            "ok": true, "principal": principal, "kind": "local",
            "admin": admin,
        }))
    }

    // ---- account_set_logon_rights -------------------------------------------

    /// Render kenny's sshd drop-in for a whole deny list.
    ///
    /// The file is owned entirely by kenny and rewritten wholesale, so it can never
    /// accumulate state or damage an operator's own configuration. An empty list
    /// still writes a file (with no `DenyUsers` line) rather than deleting it, so the
    /// "kenny manages this" marker stays visible on the machine.
    pub fn render_ssh_dropin(denied: &[String]) -> String {
        let mut out = String::from(
            "# Managed by kenny (ADR-0047). Rewritten on every account_set_logon_rights\n\
             # call; edits here are lost. Remove kenny to remove this file.\n",
        );
        if !denied.is_empty() {
            out.push_str("DenyUsers ");
            out.push_str(&denied.join(" "));
            out.push('\n');
        }
        out
    }

    /// The new full deny list after applying `deny` to `principal`.
    ///
    /// `deny` is an absolute set for *that account*, but the drop-in is machine-wide,
    /// so the other accounts' entries have to be preserved.
    pub fn merge_ssh_denials(current: &[String], principal: &str, denied: bool) -> Vec<String> {
        let mut out: Vec<String> = current
            .iter()
            .filter(|u| u.as_str() != principal)
            .cloned()
            .collect();
        if denied {
            out.push(principal.to_string());
        }
        out.sort();
        out
    }

    pub fn set_logon_rights(
        principal: &str,
        deny: &[String],
    ) -> Result<Value, (ErrorCode, String)> {
        // `network` is refused by name rather than silently dropped, the same stance
        // `validate_deny_rights` takes for `interactive`: an operator who asks for it
        // is trying to do something this OS cannot do, and a quiet no-op would look
        // like it worked. (The guard already refuses it from the inventory; this is
        // the belt-and-braces check for a host whose probe said otherwise.)
        if deny.iter().any(|r| r == "network") {
            return Err((
                ErrorCode::Unsupported,
                "denying network sign-in is not possible on Linux: there is no \
                 per-account network-logon plane, only SSH (deny remote_interactive)"
                    .to_string(),
            ));
        }

        let current = std::fs::read_to_string(inv::SSHD_DROPIN)
            .map(|text| inv::parse_deny_users(&text))
            .unwrap_or_default();
        let merged = merge_ssh_denials(
            &current,
            principal,
            deny.iter().any(|r| r == "remote_interactive"),
        );
        let rendered = render_ssh_dropin(&merged);

        let previous = std::fs::read_to_string(inv::SSHD_DROPIN).ok();
        std::fs::create_dir_all(inv::SSHD_DROPIN_DIR).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!(
                    "account_set_logon_rights: cannot create {}: {e}",
                    inv::SSHD_DROPIN_DIR
                ),
            )
        })?;
        std::fs::write(inv::SSHD_DROPIN, &rendered).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!(
                    "account_set_logon_rights: cannot write {}: {e}",
                    inv::SSHD_DROPIN
                ),
            )
        })?;

        // Validate before reloading, and roll back if sshd rejects the result. A
        // config that is written but not live would let kenny report success for a
        // restriction that is not in force; a config that is live and broken would
        // strand a headless machine.
        let restore = |previous: &Option<String>| match previous {
            Some(text) => {
                let _ = std::fs::write(inv::SSHD_DROPIN, text);
            }
            None => {
                let _ = std::fs::remove_file(inv::SSHD_DROPIN);
            }
        };
        if let Err(err) = run(&argv(&["sshd", "-t"]), "account_set_logon_rights") {
            restore(&previous);
            return Err(err);
        }
        // Debian calls the unit `ssh`, RHEL calls it `sshd`; try both before failing.
        let reloaded = ["ssh", "sshd"].iter().any(|unit| {
            run(
                &argv(&["systemctl", "reload", unit]),
                "account_set_logon_rights",
            )
            .is_ok()
        });
        if !reloaded {
            restore(&previous);
            return Err((
                ErrorCode::ExecFailed,
                "account_set_logon_rights: wrote the sshd drop-in but could not reload \
                 sshd, so the restriction is not in force; rolled back"
                    .to_string(),
            ));
        }

        Ok(json!({
            "ok": true, "principal": principal, "kind": "local",
            "deny": deny,
        }))
    }

    // ---- account_create ------------------------------------------------------

    pub fn plan_create(name: &str, display_name: &str) -> Vec<String> {
        let mut out = argv(&["useradd", "--create-home"]);
        if !display_name.is_empty() {
            out.push("--comment".to_string());
            out.push(display_name.to_string());
        }
        // A login shell, or the account would be created already disabled.
        out.push("--shell".to_string());
        out.push(
            if std::path::Path::new("/bin/bash").exists() {
                "/bin/bash"
            } else {
                "/bin/sh"
            }
            .to_string(),
        );
        out.push(name.to_string());
        out
    }

    pub fn create(
        name: &str,
        password: &str,
        display_name: &str,
        admin: bool,
    ) -> Result<Value, (ErrorCode, String)> {
        run(&plan_create(name, display_name), "account_create")?;
        // The password goes in on stdin, never in argv: `/proc/<pid>/cmdline` is
        // world-readable, so a password as an argument is visible to every user on
        // the machine for as long as the process lives.
        set_password_via_stdin(name, password)?;
        if admin {
            let group_file = std::fs::read_to_string("/etc/group").unwrap_or_default();
            for command in plan_set_admin(name, true, &group_file)? {
                run(&command, "account_create")?;
            }
        }
        Ok(json!({ "ok": true, "principal": name, "kind": "local" }))
    }

    fn set_password_via_stdin(name: &str, password: &str) -> Result<(), (ErrorCode, String)> {
        use std::io::Write;
        use std::process::Stdio;

        let mut child = Command::new("chpasswd")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| {
                (
                    ErrorCode::ExecFailed,
                    format!("account_create: could not run chpasswd: {e}"),
                )
            })?;
        {
            let stdin = child.stdin.as_mut().ok_or_else(|| {
                (
                    ErrorCode::Internal,
                    "account_create: chpasswd stdin unavailable".to_string(),
                )
            })?;
            // Deliberately not logged, not echoed, not in argv.
            stdin
                .write_all(format!("{name}:{password}\n").as_bytes())
                .map_err(|e| {
                    (
                        ErrorCode::ExecFailed,
                        format!("account_create: could not write to chpasswd: {e}"),
                    )
                })?;
        }
        let out = child.wait_with_output().map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("account_create: chpasswd did not finish: {e}"),
            )
        })?;
        if out.status.success() {
            return Ok(());
        }
        Err((
            ErrorCode::ExecFailed,
            format!(
                "account_create: chpasswd failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ),
        ))
    }

    // ---- account_delete ------------------------------------------------------

    /// `userdel`, with `--remove` for the home directory and mail spool.
    ///
    /// Deliberately **without** `--force`: deleting an account that is currently
    /// signed in leaves the system in an inconsistent state, and "user is currently
    /// used by process N" is an actionable message the operator should see.
    pub fn plan_delete(principal: &str, remove_profile: bool) -> Vec<String> {
        let mut out = argv(&["userdel"]);
        if remove_profile {
            out.push("--remove".to_string());
        }
        out.push(principal.to_string());
        out
    }

    pub fn delete(principal: &str, remove_profile: bool) -> Result<Value, (ErrorCode, String)> {
        run(&plan_delete(principal, remove_profile), "account_delete")?;
        Ok(json!({
            "ok": true, "principal": principal, "profile_removed": remove_profile,
        }))
    }

    // ---- account_session_action ---------------------------------------------

    /// One logind session belonging to the principal.
    #[derive(Debug, PartialEq, Eq)]
    pub struct LogindSession {
        pub id: String,
        pub state: String,
        pub graphical: bool,
    }

    /// Parse `loginctl show-session` output for one session.
    ///
    /// `Type` is `x11`/`wayland` for a desktop and `tty` for a console or SSH login;
    /// only the former can be *locked*, which is why the distinction is carried.
    pub fn parse_show_session(id: &str, text: &str) -> Option<(String, LogindSession)> {
        let mut name = None;
        let (mut state, mut kind) = (String::new(), String::new());
        for line in text.lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            match key.trim() {
                "Name" => name = Some(value.trim().to_string()),
                "State" => state = value.trim().to_string(),
                "Type" => kind = value.trim().to_string(),
                _ => {}
            }
        }
        Some((
            name?,
            LogindSession {
                id: id.to_string(),
                state,
                graphical: kind == "x11" || kind == "wayland" || kind == "mir",
            },
        ))
    }

    /// Session ids from `loginctl list-sessions --no-legend`, whose first column is
    /// the id. Ids are opaque strings (`2`, `c1`), never numbers.
    pub fn parse_session_ids(text: &str) -> Vec<String> {
        text.lines()
            .filter_map(|line| line.split_whitespace().next())
            .map(str::to_string)
            .collect()
    }

    fn sessions_for(principal: &str) -> Vec<LogindSession> {
        let Ok(listed) = Command::new("loginctl")
            .args(["list-sessions", "--no-legend"])
            .output()
        else {
            return Vec::new();
        };
        parse_session_ids(&String::from_utf8_lossy(&listed.stdout))
            .into_iter()
            .filter_map(|id| {
                let out = Command::new("loginctl")
                    .args([
                        "show-session",
                        &id,
                        "-p",
                        "Name",
                        "-p",
                        "State",
                        "-p",
                        "Type",
                    ])
                    .output()
                    .ok()?;
                let (name, session) =
                    parse_show_session(&id, &String::from_utf8_lossy(&out.stdout))?;
                (name == principal).then_some(session)
            })
            .collect()
    }

    /// Lock or end every session belonging to `principal`.
    ///
    /// There is no `warn_seconds` equivalent on Linux — `wall` is machine-wide,
    /// `write` needs a tty with `mesg y`, and `notify-send` needs the user's own
    /// D-Bus session bus. Rather than send a warning that may silently vanish, the
    /// inventory publishes `session_warn` as unsupported and this acts immediately
    /// (ADR-0047).
    pub async fn session_action(
        principal: &str,
        action: &str,
    ) -> Result<Value, (ErrorCode, String)> {
        let sessions = sessions_for(principal);
        if sessions.is_empty() {
            return Ok(json!({
                "ok": true, "principal": principal, "action": action, "sessions": [],
            }));
        }
        let acted: Vec<Value> = sessions
            .iter()
            .map(|session| {
                // `lock-session` on a tty is a silent no-op in logind, so a
                // non-graphical session reports `acted: false` rather than a success
                // the operator cannot verify.
                let ok = if action == "lock" {
                    session.graphical
                        && run(
                            &argv(&["loginctl", "lock-session", &session.id]),
                            "account_session_action",
                        )
                        .is_ok()
                } else {
                    run(
                        &argv(&["loginctl", "terminate-session", &session.id]),
                        "account_session_action",
                    )
                    .is_ok()
                };
                json!({ "session_id": session.id, "state": session.state, "acted": ok })
            })
            .collect();
        Ok(json!({
            "ok": acted.iter().all(|s| s["acted"] == true),
            "principal": principal, "action": action, "sessions": acted,
        }))
    }

    // ---- password_policy_set -------------------------------------------------

    /// Where each policy field is written, and under what name.
    ///
    /// `min_length` goes to `pwquality.conf`, **not** to `login.defs PASS_MIN_LEN`:
    /// PAM ignores the latter, so writing it would describe a rule nothing enforces.
    fn policy_target(key: &str) -> Option<(&'static str, &'static str)> {
        match key {
            "min_length" => Some(("/etc/security/pwquality.conf", "minlen")),
            "max_age_days" => Some(("/etc/login.defs", "PASS_MAX_DAYS")),
            "lockout_threshold" => Some(("/etc/security/faillock.conf", "deny")),
            _ => None,
        }
    }

    /// Rewrite `setting` to `value` in a `key = value` / `key value` config file.
    ///
    /// An existing (uncommented) line is replaced in place, preserving its
    /// separator style; otherwise the setting is appended with a marker. Commented
    /// lines are left alone so the file's own documentation survives.
    pub fn upsert_setting(text: &str, setting: &str, value: u32, separator: &str) -> String {
        let mut replaced = false;
        let mut out: Vec<String> = text
            .lines()
            .map(|line| {
                let bare = line.split('#').next().unwrap_or("").trim();
                let matches_key = bare.strip_prefix(setting).is_some_and(|rest| {
                    rest.is_empty() || rest.starts_with(|c: char| c.is_whitespace() || c == '=')
                });
                if matches_key {
                    replaced = true;
                    format!("{setting}{separator}{value}")
                } else {
                    line.to_string()
                }
            })
            .collect();
        if !replaced {
            out.push("# set by kenny".to_string());
            out.push(format!("{setting}{separator}{value}"));
        }
        let mut joined = out.join("\n");
        joined.push('\n');
        joined
    }

    pub fn password_policy_set(
        fields: &[(&'static str, u32)],
    ) -> Result<Value, (ErrorCode, String)> {
        let mut applied = serde_json::Map::new();
        for (key, value) in fields {
            let Some((path, setting)) = policy_target(key) else {
                continue;
            };
            let existing = std::fs::read_to_string(path).unwrap_or_default();
            // `login.defs` is whitespace-separated; the PAM files use `=`.
            let separator = if path == "/etc/login.defs" {
                "\t"
            } else {
                " = "
            };
            let updated = upsert_setting(&existing, setting, *value, separator);
            std::fs::write(path, updated).map_err(|e| {
                (
                    ErrorCode::ExecFailed,
                    format!("password_policy_set: cannot write {path}: {e}"),
                )
            })?;
            applied.insert((*key).to_string(), Value::from(*value));

            // `login.defs` only governs accounts created from now on, so an operator
            // setting a maximum age would otherwise change nothing for the people
            // actually using the machine. Apply it to the existing accounts too —
            // except root, whose password expiring on a headless box is how you lose
            // the machine.
            if *key == "max_age_days" {
                for name in existing_non_root_accounts() {
                    let _ = run(
                        &argv(&["chage", "-M", &value.to_string(), &name]),
                        "password_policy_set",
                    );
                }
            }
        }
        Ok(json!({
            "ok": true,
            "applies_to": "local_only",
            "policy": Value::Object(applied),
        }))
    }

    fn existing_non_root_accounts() -> Vec<String> {
        std::fs::read_to_string("/etc/passwd")
            .map(|passwd| inv::login_account_names(&passwd))
            .unwrap_or_default()
            .into_iter()
            .filter(|n| n != "root")
            .collect()
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        const GROUP: &str = "root:x:0:\nsudo:x:27:papa\nwheel:x:998:papa\n";

        #[test]
        fn disabling_sets_an_expiry_not_just_a_password_lock() {
            let plan = plan_set_enabled("kid", false);
            assert_eq!(plan[0], argv(&["usermod", "--expiredate", "1", "kid"]));
            assert_eq!(plan[1], argv(&["usermod", "-L", "kid"]));

            let plan = plan_set_enabled("kid", true);
            assert_eq!(plan[0], argv(&["usermod", "--expiredate", "", "kid"]));
            assert_eq!(plan[1], argv(&["usermod", "-U", "kid"]));
        }

        #[test]
        fn demotion_strips_every_admin_group_promotion_picks_one() {
            let promote = plan_set_admin("kid", true, GROUP).unwrap();
            assert_eq!(promote, vec![argv(&["gpasswd", "--add", "kid", "sudo"])]);

            // papa is in both sudo and wheel: leaving either behind would report a
            // demotion that did not happen.
            let demote = plan_set_admin("papa", false, GROUP).unwrap();
            assert_eq!(
                demote,
                vec![
                    argv(&["gpasswd", "--delete", "papa", "sudo"]),
                    argv(&["gpasswd", "--delete", "papa", "wheel"]),
                ]
            );

            // Nothing to strip is a valid, empty plan — not an error.
            assert!(plan_set_admin("kid", false, GROUP).unwrap().is_empty());

            // No admin group at all is refused rather than guessed at.
            let err = plan_set_admin("kid", true, "users:x:100:\n").unwrap_err();
            assert_eq!(err.0, ErrorCode::Unsupported);
        }

        #[test]
        fn ssh_dropin_is_rewritten_wholesale_and_preserves_other_accounts() {
            let current = vec!["kid".to_string(), "guest".to_string()];
            // Denying papa must not release kid or guest.
            let merged = merge_ssh_denials(&current, "papa", true);
            assert_eq!(merged, vec!["guest", "kid", "papa"]);
            // Clearing papa's denial leaves the others in place.
            let merged = merge_ssh_denials(&merged, "papa", false);
            assert_eq!(merged, vec!["guest", "kid"]);
            // Denying an already-denied account is idempotent.
            assert_eq!(
                merge_ssh_denials(&merged, "kid", true),
                vec!["guest", "kid"]
            );

            let rendered = render_ssh_dropin(&merged);
            assert!(rendered.contains("DenyUsers guest kid"));
            assert!(rendered.starts_with("# Managed by kenny"));
            // Round-trips through the reader the collector uses.
            assert_eq!(inv::parse_deny_users(&rendered), vec!["guest", "kid"]);

            // An empty list still writes the marker, without a DenyUsers line.
            let empty = render_ssh_dropin(&[]);
            assert!(!empty.contains("DenyUsers"));
            assert!(inv::parse_deny_users(&empty).is_empty());
        }

        #[test]
        fn create_never_puts_the_password_in_argv() {
            let plan = plan_create("guest-visit", "Visitor");
            assert!(plan.contains(&"--create-home".to_string()));
            assert!(plan.contains(&"Visitor".to_string()));
            assert_eq!(plan.last().unwrap(), "guest-visit");
            // The whole point: nothing in the argv can be the password.
            assert!(!plan.iter().any(|a| a.contains("password")));
        }

        #[test]
        fn delete_never_forces() {
            assert_eq!(plan_delete("kid", false), argv(&["userdel", "kid"]));
            assert_eq!(
                plan_delete("kid", true),
                argv(&["userdel", "--remove", "kid"])
            );
            // `--force` would delete a signed-in user and leave the system
            // inconsistent; the failure message is more useful than the deletion.
            assert!(!plan_delete("kid", true).contains(&"--force".to_string()));
        }

        #[test]
        fn logind_sessions_are_parsed_with_opaque_string_ids() {
            let ids = parse_session_ids("   2 1000 papa seat0 tty2\n  c1 1001 kid  -     -\n");
            assert_eq!(ids, vec!["2", "c1"]);

            let (name, session) =
                parse_show_session("2", "Name=papa\nState=active\nType=wayland\n").unwrap();
            assert_eq!(name, "papa");
            assert_eq!(session.id, "2");
            assert_eq!(session.state, "active");
            assert!(session.graphical, "a wayland session can be locked");

            let (_, tty) = parse_show_session("c1", "Name=kid\nState=online\nType=tty\n").unwrap();
            assert!(!tty.graphical, "locking a tty session is a silent no-op");

            // A session with no Name is not attributable and is dropped.
            assert!(parse_show_session("3", "State=closing\n").is_none());
        }

        #[test]
        fn policy_settings_are_upserted_without_disturbing_comments() {
            let pwquality = "# minlen = 9\n# Do not edit\nminlen = 8\nretry = 3\n";
            let out = upsert_setting(pwquality, "minlen", 12, " = ");
            assert!(out.contains("minlen = 12"));
            assert!(out.contains("# minlen = 9"), "documentation survives");
            assert!(out.contains("retry = 3"), "other settings survive");
            assert_eq!(out.matches("minlen = 12").count(), 1);

            // Absent settings are appended, with a marker saying who wrote them.
            let out = upsert_setting("retry = 3\n", "minlen", 12, " = ");
            assert!(out.contains("# set by kenny"));
            assert!(out.contains("minlen = 12"));

            // login.defs is whitespace-separated.
            let out = upsert_setting("PASS_MAX_DAYS\t99999\n", "PASS_MAX_DAYS", 90, "\t");
            assert!(out.contains("PASS_MAX_DAYS\t90"));

            // min_length goes to pwquality, not to the login.defs key PAM ignores.
            assert_eq!(
                policy_target("min_length"),
                Some(("/etc/security/pwquality.conf", "minlen"))
            );
        }
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
            Action::DenyLogon("remote_interactive"),
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
            Action::DenyLogon("remote_interactive"),
            Action::Delete,
        ] {
            let err = guard(action, "papa", &inventory).unwrap_err();
            assert_eq!(err.0, ErrorCode::Blocked, "{action:?} must be blocked");
            assert!(err.1.contains("last enabled administrator"));
        }
        // A session action stays allowed: signing back in undoes it, so it cannot
        // lock anyone out.
        assert!(guard(Action::Session("lock"), "papa", &inventory).is_ok());
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

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[tokio::test]
    async fn on_an_unimplemented_os_every_tool_is_unsupported() {
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
            assert!(message.contains("operating system"), "{message}");
        }
    }

    #[test]
    fn guard_refuses_exactly_what_the_inventory_says_is_unsupported() {
        // The rule that keeps the published capability set and the enforced one from
        // drifting apart: they are the same list, read at call time (ADR-0047).
        let mut root = account("root", true, true);
        root["builtin_admin"] = json!(true);
        root["unsupported"] = json!({
            "set_admin": "root_account",
            "set_enabled": "root_account",
        });
        let mut kid = account("kid", true, false);
        kid["unsupported"] = json!({
            "deny_network": "no_network_logon_concept",
            "session_lock": "no_graphical_session",
        });
        let inventory = vec![root, kid, account("papa", true, true)];

        for (action, principal, reason) in [
            (Action::Demote, "root", "root_account"),
            (Action::Disable, "root", "root_account"),
            (
                Action::DenyLogon("network"),
                "kid",
                "no_network_logon_concept",
            ),
            (Action::Session("lock"), "kid", "no_graphical_session"),
        ] {
            let (code, message) = guard(action, principal, &inventory).unwrap_err();
            assert_eq!(code, ErrorCode::Blocked, "{action:?} on {principal}");
            assert!(message.contains(reason), "{message}");
        }

        // The verbs the map does not name stay available — negation, not enumeration.
        assert!(guard(Action::Delete, "kid", &inventory).is_ok());
        assert!(guard(Action::DenyLogon("remote_interactive"), "kid", &inventory).is_ok());
        assert!(guard(Action::Session("logoff"), "kid", &inventory).is_ok());
        // And a Windows inventory, whose only token maps to no tool, is unaffected.
        let mut msa = account("msa", true, false);
        msa["unsupported"] = json!({ "reset_password": "password_in_cloud" });
        assert!(guard(Action::Disable, "msa", &[msa]).is_ok());
    }

    #[test]
    fn new_account_names_reject_shell_hostile_shapes() {
        for ok in ["kid", "guest-visit", "svc_1", "a.b"] {
            assert!(validate_new_account_name(ok).is_ok(), "{ok}");
        }
        // A leading dash becomes an option for every POSIX tool; the rest are shell
        // metacharacters that no arm should have to be the last defence against.
        for bad in [
            "-rf",
            "kid; rm -rf /",
            "kid$(id)",
            "kid kid",
            "a/b",
            "kid`id`",
        ] {
            assert_eq!(
                validate_new_account_name(bad).unwrap_err().0,
                ErrorCode::BadArgs,
                "{bad}"
            );
        }
        assert_eq!(
            validate_new_account_name(&"a".repeat(33)).unwrap_err().0,
            ErrorCode::BadArgs
        );
    }
}
