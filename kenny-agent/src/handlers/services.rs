//! `service_restart` — restart a named Windows service or systemd unit.
//!
//! The name is validated against the contract's pattern and never reaches a shell. On
//! Linux it must be a bare unit name or end in `.service` (no other unit type), and it is
//! handed to an absolute `systemctl` after `--`, with the environment cleared. On Windows
//! the service control manager stops it (bounded wait), then starts it (bounded wait).
//!
//! Only a running service is restarted: one that is not running is refused
//! (`exec_failed`), never started. Dependents are never force-stopped: on Windows the SCM
//! refuses the stop while they run; on Linux, where `systemctl restart` would restart
//! them along, the call is refused while any unit that requires, binds to, is part of or
//! is upheld by the unit is active. The whole call is bounded by [`RESTART_BUDGET`].
//!
//! Self-protection: the agent refuses (`blocked`) to restart its own service, by its
//! built-in name ([`crate::config::SERVICE_NAME`]), by the name it was started under
//! (`run-service --service-name`, recorded once at startup) and, on Linux, by the unit
//! its own cgroup says it runs in. The call shares the per-host lock with `fs_cleanup`.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::{json, Value};

use crate::handlers::cleanup::{run_exclusive, WORKER_GRACE};
use crate::protocol::ErrorCode;

/// Total bound of one call: stop (≤ 30 s) + start (≤ 30 s) + state queries.
pub const RESTART_BUDGET: Duration = Duration::from_secs(70);

/// Longest wait for the service to stop.
pub const STOP_WAIT: Duration = Duration::from_secs(30);

/// Longest wait for the service to start.
pub const START_WAIT: Duration = Duration::from_secs(30);

// The two waits and the state queries fit the total bound.
const _: () = assert!(STOP_WAIT.as_secs() + START_WAIT.as_secs() < RESTART_BUDGET.as_secs());

/// The service name this process was started under, recorded once at startup.
static OWN_SERVICE_NAME: OnceLock<String> = OnceLock::new();

/// Record the name the agent runs under as a service (`run-service --service-name`).
pub fn record_own_service_name(name: &str) {
    let _ = OWN_SERVICE_NAME.set(name.to_string());
}

/// Maximum name length the contract's pattern admits.
const MAX_NAME: usize = 256;

/// systemd unit types other than `service`: a name with one of these suffixes is refused.
const NON_SERVICE_UNIT_TYPES: &[&str] = &[
    "socket",
    "target",
    "timer",
    "mount",
    "automount",
    "swap",
    "path",
    "slice",
    "scope",
    "device",
];

/// The refusal for a service or unit that is not running.
fn not_running(name: &str) -> (ErrorCode, String) {
    (
        ErrorCode::ExecFailed,
        format!("{name} is not running; restart does not start a stopped service"),
    )
}

/// The systemd properties listing the units a restart of a unit propagates to.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const DEPENDENT_PROPERTIES: &str = "RequiredBy,BoundBy,ConsistsOf,UpheldBy";

/// The units named by `systemctl show -p <DEPENDENT_PROPERTIES> --value` output
/// (space-separated lists, one property per line), deduplicated in order. A name that does
/// not match the contract's pattern cannot be checked safely and fails the call.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn parse_dependents(doc: &str) -> Result<Vec<String>, (ErrorCode, String)> {
    let mut out: Vec<String> = Vec::new();
    for name in doc.split_whitespace() {
        if !matches_contract_pattern(name) {
            return Err((
                ErrorCode::ExecFailed,
                "a dependent unit has a name that cannot be checked; refusing to restart"
                    .to_string(),
            ));
        }
        if !out.iter().any(|n| n == name) {
            out.push(name.to_string());
        }
    }
    Ok(out)
}

/// Refuse when any of `dependents` is active, given `systemctl is-active -- <dependents>`
/// output (one state per line, in order). Output that does not line up with the names
/// fails the call: unverified dependents are treated as running.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn check_dependents(
    unit: &str,
    dependents: &[String],
    states: &str,
) -> Result<(), (ErrorCode, String)> {
    let states: Vec<&str> = states.lines().map(str::trim).collect();
    if states.len() != dependents.len() {
        return Err((
            ErrorCode::ExecFailed,
            format!(
                "cannot tell whether units depending on {unit} are running; refusing to restart"
            ),
        ));
    }
    let active: Vec<&str> = dependents
        .iter()
        .zip(&states)
        .filter(|(_, st)| matches!(**st, "active" | "activating" | "reloading" | "refreshing"))
        .map(|(n, _)| n.as_str())
        .collect();
    if active.is_empty() {
        Ok(())
    } else {
        Err((
            ErrorCode::ExecFailed,
            format!(
                "units depending on {unit} are running ({}); stop them first",
                active.join(", ")
            ),
        ))
    }
}

/// `^[A-Za-z0-9][A-Za-z0-9_.@:\-$]{0,255}$`.
pub fn matches_contract_pattern(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= MAX_NAME
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"_.@:-$".contains(b))
}

/// Validate `name` for the given OS family. On Linux returns the unit to act on (always
/// with `.service`); on Windows the name unchanged.
pub fn validate(name: &str, linux: bool) -> Result<String, (ErrorCode, String)> {
    if !matches_contract_pattern(name) {
        return Err((
            ErrorCode::BadArgs,
            "`name` must match ^[A-Za-z0-9][A-Za-z0-9_.@:\\-$]{0,255}$".to_string(),
        ));
    }
    if !linux {
        return Ok(name.to_string());
    }
    if name.ends_with(".service") {
        if name.len() == ".service".len() {
            return Err((ErrorCode::BadArgs, "empty unit name".to_string()));
        }
        return Ok(name.to_string());
    }
    if let Some((_, suffix)) = name.rsplit_once('.') {
        if NON_SERVICE_UNIT_TYPES.contains(&suffix) {
            return Err((
                ErrorCode::BadArgs,
                "only service units (bare or `.service`) can be restarted".to_string(),
            ));
        }
    }
    Ok(format!("{name}.service"))
}

/// Comparison form of a service name: lowercase, one `.service` suffix stripped.
fn normalise(name: &str) -> String {
    let n = name.trim().to_ascii_lowercase();
    n.strip_suffix(".service").map(str::to_string).unwrap_or(n)
}

/// The systemd unit a `/proc/self/cgroup` document places this process in, if it is a
/// service (`0::/system.slice/kenny-agent.service`, or a v1 `name=systemd` line).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn unit_from_cgroup(doc: &str) -> Option<String> {
    doc.lines()
        .filter_map(|line| line.splitn(3, ':').nth(2))
        .flat_map(|path| path.rsplit('/'))
        .find(|comp| comp.ends_with(".service"))
        .map(str::to_string)
}

#[cfg(target_os = "linux")]
fn own_cgroup_unit() -> Option<&'static str> {
    static UNIT: OnceLock<Option<String>> = OnceLock::new();
    UNIT.get_or_init(|| {
        std::fs::read_to_string("/proc/self/cgroup")
            .ok()
            .and_then(|d| unit_from_cgroup(&d))
    })
    .as_deref()
}

#[cfg(not(target_os = "linux"))]
fn own_cgroup_unit() -> Option<&'static str> {
    None
}

/// Whether `name` designates the agent's own service under any of its names.
pub fn is_self(name: &str, recorded: Option<&str>, cgroup_unit: Option<&str>) -> bool {
    let n = normalise(name);
    n == normalise(crate::config::SERVICE_NAME)
        || recorded.is_some_and(|r| n == normalise(r))
        || cgroup_unit.is_some_and(|u| n == normalise(u))
}

fn refuse_self(name: &str) -> Result<(), (ErrorCode, String)> {
    if is_self(
        name,
        OWN_SERVICE_NAME.get().map(String::as_str),
        own_cgroup_unit(),
    ) {
        return Err((
            ErrorCode::Blocked,
            "refused by agent safety policy: the agent does not restart its own service"
                .to_string(),
        ));
    }
    Ok(())
}

/// `service_restart` — `{name}` → `{ok, name, was_running, running, waited_ms}`.
pub async fn restart(args: Value) -> Result<Value, (ErrorCode, String)> {
    if !(cfg!(windows) || cfg!(target_os = "linux")) {
        return Err((
            ErrorCode::Unsupported,
            "service_restart is not supported on this platform".to_string(),
        ));
    }
    let Some(name) = args.get("name").and_then(Value::as_str) else {
        return Err((ErrorCode::BadArgs, "`name` must be a string".to_string()));
    };
    let target = validate(name, cfg!(target_os = "linux"))?;
    refuse_self(name)?;
    refuse_self(&target)?;
    let name = name.to_string();
    run_exclusive(
        RESTART_BUDGET + WORKER_GRACE,
        "the service restart",
        move |cancel| {
            platform::restart(&target, &cancel).map(|r| {
                json!({
                    "ok": r.ok,
                    "name": name,
                    "was_running": r.was_running,
                    "running": r.running,
                    "waited_ms": r.waited_ms,
                })
            })
        },
    )
    .await
}

/// The refusal of a call the handler has already given up on.
#[cfg_attr(not(any(windows, target_os = "linux")), allow(dead_code))]
fn abandoned() -> (ErrorCode, String) {
    (
        ErrorCode::Timeout,
        "the service restart was abandoned".to_string(),
    )
}

/// What a restart observed.
#[cfg_attr(not(any(windows, target_os = "linux")), allow(dead_code))]
struct Outcome {
    ok: bool,
    was_running: bool,
    running: bool,
    waited_ms: u64,
}

#[cfg(target_os = "linux")]
mod platform {
    use std::path::Path;
    use std::time::{Duration, Instant};

    use super::{
        abandoned, check_dependents, not_running, parse_dependents, Outcome, DEPENDENT_PROPERTIES,
        RESTART_BUDGET, START_WAIT, STOP_WAIT,
    };
    use crate::handlers::cleanup::{run_fixed, Cancel};
    use crate::protocol::ErrorCode;
    use crate::telemetry::collectors::ProbeFailure;

    /// Bound of a state query.
    const QUERY_WAIT: Duration = Duration::from_secs(5);

    fn systemctl() -> Result<&'static str, (ErrorCode, String)> {
        ["/usr/bin/systemctl", "/bin/systemctl"]
            .into_iter()
            .find(|p| Path::new(p).is_file())
            .ok_or((ErrorCode::Unsupported, "systemctl not found".to_string()))
    }

    /// Time left before `deadline`, capped at `cap`.
    fn left(deadline: Instant, cap: Duration) -> Duration {
        deadline.saturating_duration_since(Instant::now()).min(cap)
    }

    fn query(
        systemctl: &str,
        args: &[&str],
        deadline: Instant,
    ) -> Result<String, (ErrorCode, String)> {
        match run_fixed(systemctl, args, left(deadline, QUERY_WAIT)) {
            Ok(out) => Ok(out.stdout.trim().to_string()),
            Err(ProbeFailure::Timeout(_)) => Err((
                ErrorCode::Timeout,
                "systemctl did not answer in time".to_string(),
            )),
            Err(_) => Err((
                ErrorCode::ExecFailed,
                "systemctl could not be started".to_string(),
            )),
        }
    }

    fn is_active(
        systemctl: &str,
        unit: &str,
        deadline: Instant,
    ) -> Result<bool, (ErrorCode, String)> {
        Ok(query(systemctl, &["is-active", "--", unit], deadline)? == "active")
    }

    pub(super) fn restart(unit: &str, cancel: &Cancel) -> Result<Outcome, (ErrorCode, String)> {
        let systemctl = systemctl()?;
        let deadline = Instant::now() + RESTART_BUDGET;
        let load = query(
            systemctl,
            &["show", "--property=LoadState", "--value", "--", unit],
            deadline,
        )?;
        if load == "not-found" || load.is_empty() {
            return Err((ErrorCode::NotFound, format!("unit {unit} not found")));
        }
        if !is_active(systemctl, unit, deadline)? {
            return Err(not_running(unit));
        }
        // `systemctl restart` propagates to these: refuse while any of them is active.
        let property = format!("--property={DEPENDENT_PROPERTIES}");
        let listed = query(
            systemctl,
            &["show", &property, "--value", "--", unit],
            deadline,
        )?;
        let dependents = parse_dependents(&listed)?;
        if !dependents.is_empty() {
            let mut args = vec!["is-active", "--"];
            args.extend(dependents.iter().map(String::as_str));
            let states = query(systemctl, &args, deadline)?;
            check_dependents(unit, &dependents, &states)?;
        }
        let Some(permit) = cancel.permit() else {
            return Err(abandoned());
        };
        let started = Instant::now();
        // Leave room for the final state query inside the total bound. `try-restart`
        // leaves a unit that stopped in the meantime stopped.
        let restart_wait = left(deadline - QUERY_WAIT, STOP_WAIT + START_WAIT);
        let restarted = run_fixed(systemctl, &["try-restart", "--", unit], restart_wait);
        drop(permit);
        let waited_ms = started.elapsed().as_millis() as u64;
        match restarted {
            Ok(out) if out.success => {}
            Ok(out) => {
                let code = out
                    .code
                    .map_or_else(|| "a signal".to_string(), |c| c.to_string());
                return Err((
                    ErrorCode::ExecFailed,
                    format!("systemctl try-restart {unit} failed (exit {code})"),
                ));
            }
            Err(ProbeFailure::Timeout(_)) => {
                return Err((
                    ErrorCode::Timeout,
                    format!("{unit} did not finish restarting within 60 s"),
                ))
            }
            Err(_) => {
                return Err((
                    ErrorCode::ExecFailed,
                    "systemctl could not be started".to_string(),
                ))
            }
        }
        let running = is_active(systemctl, unit, deadline)?;
        Ok(Outcome {
            ok: running,
            was_running: true,
            running,
            waited_ms,
        })
    }
}

#[cfg(windows)]
mod platform {
    use std::ffi::OsStr;
    use std::time::{Duration, Instant};

    use windows_service::service::{Service, ServiceAccess, ServiceState};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    use super::{abandoned, not_running, Outcome, RESTART_BUDGET, START_WAIT, STOP_WAIT};
    use crate::handlers::cleanup::Cancel;
    use crate::protocol::ErrorCode;

    const POLL: Duration = Duration::from_millis(250);

    fn os_error(e: &windows_service::Error) -> Option<i32> {
        match e {
            windows_service::Error::Winapi(io) => io.raw_os_error(),
            _ => None,
        }
    }

    fn state(svc: &Service) -> Result<ServiceState, (ErrorCode, String)> {
        svc.query_status().map(|s| s.current_state).map_err(|_| {
            (
                ErrorCode::ExecFailed,
                "cannot query the service".to_string(),
            )
        })
    }

    /// Time left before `deadline`, capped at `cap`.
    fn left(deadline: Instant, cap: Duration) -> Duration {
        deadline.saturating_duration_since(Instant::now()).min(cap)
    }

    /// Poll until `want` or `max` elapses; the final state either way.
    fn wait_for(
        svc: &Service,
        want: ServiceState,
        max: Duration,
    ) -> Result<ServiceState, (ErrorCode, String)> {
        let deadline = Instant::now() + max;
        loop {
            let st = state(svc)?;
            if st == want || Instant::now() >= deadline {
                return Ok(st);
            }
            std::thread::sleep(POLL);
        }
    }

    /// Stop, then start, a running service, all within [`RESTART_BUDGET`]: each wait is
    /// capped by its own bound and by what is left of the total. Once the service has
    /// stopped it is always started again, even after the handler has given up on the
    /// call; no stop is requested after that.
    pub(super) fn restart(name: &str, cancel: &Cancel) -> Result<Outcome, (ErrorCode, String)> {
        let deadline = Instant::now() + RESTART_BUDGET;
        let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
            .map_err(|_| {
            (
                ErrorCode::ExecFailed,
                "cannot connect to the service control manager".to_string(),
            )
        })?;
        let svc = manager
            .open_service(
                name,
                ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::START,
            )
            .map_err(|e| match os_error(&e) {
                // ERROR_SERVICE_DOES_NOT_EXIST
                Some(1060) => (ErrorCode::NotFound, format!("service {name} not found")),
                Some(5) => (
                    ErrorCode::ExecFailed,
                    format!("access to service {name} denied"),
                ),
                _ => (ErrorCode::ExecFailed, format!("cannot open service {name}")),
            })?;

        if state(&svc)? != ServiceState::Running {
            return Err(not_running(name));
        }
        let started = Instant::now();

        let Some(permit) = cancel.permit() else {
            return Err(abandoned());
        };
        match svc.stop() {
            Ok(_) => {}
            // ERROR_SERVICE_NOT_ACTIVE: it stopped on its own; it is not started.
            Err(e) if os_error(&e) == Some(1062) => return Err(not_running(name)),
            // ERROR_DEPENDENT_SERVICES_RUNNING: never force dependents.
            Err(e) if os_error(&e) == Some(1051) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("services depending on {name} are running; stop them first"),
                ))
            }
            Err(e) if os_error(&e) == Some(1052) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("service {name} does not accept a stop request"),
                ))
            }
            Err(_) => return Err((ErrorCode::ExecFailed, format!("cannot stop service {name}"))),
        }
        drop(permit);
        if wait_for(&svc, ServiceState::Stopped, left(deadline, STOP_WAIT))?
            != ServiceState::Stopped
        {
            return Err((
                ErrorCode::Timeout,
                format!("service {name} did not stop within the time limit"),
            ));
        }

        match svc.start(&[] as &[&OsStr]) {
            Ok(()) => {}
            // ERROR_SERVICE_ALREADY_RUNNING
            Err(e) if os_error(&e) == Some(1056) => {}
            // ERROR_SERVICE_DISABLED
            Err(e) if os_error(&e) == Some(1058) => {
                return Err((ErrorCode::ExecFailed, format!("service {name} is disabled")))
            }
            Err(_) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("cannot start service {name}"),
                ))
            }
        }
        let after = wait_for(&svc, ServiceState::Running, left(deadline, START_WAIT))?;
        let waited_ms = started.elapsed().as_millis() as u64;
        if after == ServiceState::StartPending {
            return Err((
                ErrorCode::Timeout,
                format!("service {name} did not start within the time limit"),
            ));
        }
        let running = after == ServiceState::Running;
        Ok(Outcome {
            ok: running,
            was_running: true,
            running,
            waited_ms,
        })
    }
}

#[cfg(not(any(windows, target_os = "linux")))]
mod platform {
    use super::Outcome;
    use crate::handlers::cleanup::Cancel;
    use crate::protocol::ErrorCode;

    pub(super) fn restart(_name: &str, _cancel: &Cancel) -> Result<Outcome, (ErrorCode, String)> {
        Err((
            ErrorCode::Unsupported,
            "service_restart is not supported on this platform".to_string(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contract_pattern_accepts_and_rejects() {
        let long_ok = format!("a{}", "b".repeat(255));
        let too_long = format!("a{}", "b".repeat(256));
        assert_eq!(long_ok.len(), 256);
        assert_eq!(too_long.len(), 257);
        for ok in [
            "spooler",
            "wuauserv",
            "a",
            "foo@bar.service",
            "x:y",
            "svc$1",
            "a-b_c.d",
            &long_ok,
        ] {
            assert!(matches_contract_pattern(ok), "{ok:?} must match");
        }
        for bad in [
            "",
            "-x",
            "a b",
            "../x",
            "x/y",
            " kenny-agent",
            "_x",
            ".x",
            "é",
            "a;b",
            &too_long,
        ] {
            assert!(!matches_contract_pattern(bad), "{bad:?} must not match");
        }
    }

    #[test]
    fn linux_accepts_bare_and_service_units_only() {
        assert_eq!(validate("nginx", true).unwrap(), "nginx.service");
        assert_eq!(validate("nginx.service", true).unwrap(), "nginx.service");
        assert_eq!(
            validate("foo@bar.service", true).unwrap(),
            "foo@bar.service"
        );
        assert_eq!(validate("getty@tty1", true).unwrap(), "getty@tty1.service");
        for bad in [
            "foo.socket",
            "multi-user.target",
            "x.timer",
            "-x",
            "a b",
            "../x",
            "-.mount",
        ] {
            assert_eq!(
                validate(bad, true).unwrap_err().0,
                ErrorCode::BadArgs,
                "{bad:?}"
            );
        }
        let too_long = format!("a{}", "b".repeat(256));
        assert_eq!(validate(&too_long, true).unwrap_err().0, ErrorCode::BadArgs);
        // Windows names are taken as given once they match the pattern.
        assert_eq!(validate("foo.socket", false).unwrap(), "foo.socket");
        assert_eq!(validate("a b", false).unwrap_err().0, ErrorCode::BadArgs);
    }

    #[test]
    fn self_is_refused_under_every_name() {
        for name in [
            "kenny-agent",
            "KENNY-AGENT",
            "kenny-agent.service",
            "Kenny-Agent.SERVICE",
        ] {
            assert!(is_self(name, None, None), "{name:?}");
        }
        assert!(!is_self("spooler", None, None));
        assert!(!is_self("kenny-agent-helper", None, None));
        // The name the agent was installed under.
        assert!(is_self("KennyCustom", Some("kennycustom"), None));
        assert!(is_self("kennycustom.service", Some("KennyCustom"), None));
        // The unit the cgroup places the process in.
        assert!(is_self("fleet-agent", None, Some("fleet-agent.service")));
        assert!(!is_self("fleet-agent", None, None));
    }

    #[test]
    fn cgroup_unit_is_parsed_for_v1_and_v2() {
        assert_eq!(
            unit_from_cgroup("0::/system.slice/kenny-agent.service\n").as_deref(),
            Some("kenny-agent.service")
        );
        assert_eq!(
            unit_from_cgroup(
                "12:pids:/system.slice/x.service\n1:name=systemd:/system.slice/custom.service\n"
            )
            .as_deref(),
            Some("x.service")
        );
        assert_eq!(
            unit_from_cgroup("0::/user.slice/user-0.slice/session-3.scope\n"),
            None
        );
        assert_eq!(unit_from_cgroup(""), None);
    }

    #[tokio::test]
    async fn handler_rejects_bad_names_and_self_before_touching_anything() {
        let err = restart(json!({})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
        let err = restart(json!({"name": 7})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
        let err = restart(json!({"name": "-x"})).await.unwrap_err();
        if cfg!(any(windows, target_os = "linux")) {
            assert_eq!(err.0, ErrorCode::BadArgs);
        } else {
            assert_eq!(err.0, ErrorCode::Unsupported);
        }
        let err = restart(json!({"name": "Kenny-Agent.service"}))
            .await
            .unwrap_err();
        if cfg!(any(windows, target_os = "linux")) {
            assert_eq!(err.0, ErrorCode::Blocked);
        }
    }

    #[cfg(not(any(windows, target_os = "linux")))]
    #[tokio::test]
    async fn unsupported_off_windows_and_linux() {
        let err = restart(json!({"name": "spooler"})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }

    #[cfg(any(windows, target_os = "linux"))]
    #[tokio::test]
    async fn busy_host_is_exec_failed() {
        let _held = crate::handlers::cleanup::hold_host_lock_for_test();
        let err = restart(json!({"name": "kenny-no-such-service-zz"}))
            .await
            .unwrap_err();
        assert_eq!(err.0, ErrorCode::ExecFailed);
        assert_eq!(err.1, crate::handlers::cleanup::BUSY_MESSAGE);
    }

    #[test]
    fn dependents_are_parsed_and_validated() {
        // One line per property, space-separated lists, empty lines for empty ones.
        let doc = "a.service b.service\n\nc.mount a.service\nd@x.service\n";
        assert_eq!(
            parse_dependents(doc).unwrap(),
            ["a.service", "b.service", "c.mount", "d@x.service"]
        );
        assert!(parse_dependents("").unwrap().is_empty());
        assert!(parse_dependents("\n\n\n").unwrap().is_empty());
        // An escaped or option-like name cannot be checked safely: refused.
        for bad in [
            "systemd-fsck@dev-disk-by\\x2duuid.service",
            "-x.service",
            "a.service --now",
        ] {
            assert_eq!(
                parse_dependents(bad).unwrap_err().0,
                ErrorCode::ExecFailed,
                "{bad:?}"
            );
        }
    }

    #[test]
    fn an_active_dependent_refuses_the_restart() {
        let deps: Vec<String> = ["a.service", "b.service"].map(String::from).to_vec();
        assert!(check_dependents("x.service", &deps, "inactive\nfailed\n").is_ok());
        assert!(check_dependents("x.service", &deps, "inactive\ndeactivating\n").is_ok());
        assert!(check_dependents("x.service", &[], "").is_ok());
        for states in [
            "active\ninactive\n",
            "inactive\nactivating\n",
            "reloading\nactive\n",
        ] {
            let err = check_dependents("x.service", &deps, states).unwrap_err();
            assert_eq!(err.0, ErrorCode::ExecFailed, "{states:?}");
            assert!(err.1.contains("stop them first"), "{}", err.1);
        }
        // Output that does not line up with the names is not trusted.
        for states in ["", "inactive\n", "inactive\ninactive\ninactive\n"] {
            assert_eq!(
                check_dependents("x.service", &deps, states).unwrap_err().0,
                ErrorCode::ExecFailed,
                "{states:?}"
            );
        }
    }

    #[test]
    fn not_running_refusal_says_restart_does_not_start() {
        let (code, msg) = not_running("nginx.service");
        assert_eq!(code, ErrorCode::ExecFailed);
        assert_eq!(
            msg,
            "nginx.service is not running; restart does not start a stopped service"
        );
    }

    /// A unit that does not exist maps to `not_found`, where systemd is there to ask.
    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn missing_unit_is_not_found_where_systemd_answers() {
        let systemd_up = std::path::Path::new("/run/systemd/system").is_dir()
            && std::path::Path::new("/usr/bin/systemctl").is_file();
        if !systemd_up {
            return;
        }
        match restart(json!({"name": "kenny-no-such-unit-zz9"})).await {
            Err((ErrorCode::NotFound, _)) => {}
            // Another test held the shared lock at that moment.
            Err((ErrorCode::ExecFailed, m)) if m == crate::handlers::cleanup::BUSY_MESSAGE => {}
            other => panic!("expected not_found, got {other:?}"),
        }
    }
}
