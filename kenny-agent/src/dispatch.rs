//! Tool dispatch: map a `request` frame to a handler and build a `response`.
//!
//! Unknown tools return `error.code = "unsupported"`. Handlers signal failure with
//! `(ErrorCode, message)`, which becomes `response.error`.

use serde_json::{json, Value};
use tracing::{debug, warn};

use crate::coexist;
use crate::control;
use crate::handlers;
use crate::policy;
use crate::protocol::{ErrorCode, Request, Response};

/// Dispatch one request and produce the response to send back.
pub async fn handle(req: Request) -> Response {
    debug!(tool = %req.tool, id = %req.id, "dispatching request");
    let result = run(&req.tool, req.args.clone()).await;
    match result {
        Ok(value) => Response::ok(req.id, value),
        Err((code, message)) => Response::err(req.id, code, message),
    }
}

/// Route a tool name to its handler.
async fn run(tool: &str, args: Value) -> Result<Value, (ErrorCode, String)> {
    // Local kill switch: if the person at the endpoint switched remote control off,
    // refuse every mutating tool. Telemetry and read-only diagnostics are unaffected.
    if control::is_mutating(tool) && !control::remote_control_enabled() {
        return Err((
            ErrorCode::Disabled,
            "remote control is disabled at the endpoint".to_string(),
        ));
    }

    // Deterministic, always-on safety guard: refuse individually dangerous calls
    // regardless of approval or kill-switch state. Cannot be disabled remotely. See
    // ADR-0020. Runs for every tool before reaching a handler. On a block, emit a
    // structured warn (forwarded to the server's event store via the `log` frame,
    // ADR-0017/0021) naming the matched rule, then refuse the call.
    if let Err((code, message)) = policy::check(tool, &args) {
        warn!(tool = %tool, reason = %message, "tool call refused by safety guard");
        return Err((code, message));
    }

    // Anti-cheat coexistence (ADR-0039): while a protected game is running, voluntarily
    // step back from the most anti-cheat-visible tools (today `screen_capture`) and
    // report `paused`. Transparent, not evasive — see `coexist`.
    if let Err((code, message)) = coexist::gate(tool) {
        debug!(tool = %tool, reason = %message, "tool paused for anti-cheat coexistence");
        return Err((code, message));
    }

    match tool {
        "powershell_exec" => handlers::powershell::exec(args).await,

        "fs_list" => handlers::fs::list(args),
        "fs_search" => handlers::fs::search(args),
        "fs_read" => handlers::fs::read(args),
        "fs_disk_usage" => handlers::fs::disk_usage(args),

        "winget_list" => handlers::winget::list(args).await,
        "winget_install" => handlers::winget::install(args).await,
        "winget_uninstall" => handlers::winget::uninstall(args).await,
        "winget_update" => handlers::winget::update(args).await,

        "diag_processes" => handlers::diagnostics::processes(args),
        "diag_services" => handlers::diagnostics::services(args),
        "diag_eventlog" => handlers::diagnostics::eventlog(args),
        "diag_autostart" => handlers::diagnostics::autostart(args),

        "net_config" => handlers::network::config(args),
        "net_dns_flush" => handlers::network::dns_flush(args).await,
        "net_adapter_reset" => handlers::network::adapter_reset(args).await,

        "screen_capture" => handlers::screenshot::capture(args),

        "remotehelp_status" => handlers::remotehelp::status(args).await,
        "remotehelp_start" => handlers::remotehelp::start(args),
        "remotehelp_stop" => handlers::remotehelp::stop(args).await,

        "webfilter_status" => handlers::webfilter::status(args).await,
        "webfilter_apply" => handlers::webfilter::apply(args).await,
        "webfilter_clear" => handlers::webfilter::clear(args).await,

        "telemetry_collect" => telemetry_collect(args),

        "agent_update" => handlers::agent_update::update(args).await,

        other => Err((ErrorCode::Unsupported, format!("unknown tool: {other}"))),
    }
}

/// `telemetry_collect` — return the snapshot map (optionally a subset of sections).
fn telemetry_collect(args: Value) -> Result<Value, (ErrorCode, String)> {
    let sections: Vec<String> = args
        .get("sections")
        .and_then(|s| s.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();
    let snapshot = crate::telemetry::collectors::collect_all(&sections);
    Ok(json!(snapshot))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Point the control file at a temp path in the given state for the closure's
    /// duration, then restore the environment. Serialized via the crate-wide
    /// `control::TEST_ENV_LOCK` so it never races other `KENNY_CONTROL_FILE` tests.
    #[allow(clippy::await_holding_lock)] // single-threaded test runtime; lock guards env
    async fn with_remote_control<F>(enabled: bool, name: &str, f: F) -> Response
    where
        F: std::future::Future<Output = Response>,
    {
        let _guard = crate::control::TEST_ENV_LOCK.lock().unwrap();
        let path = std::env::temp_dir().join(name);
        std::env::set_var(crate::control::CONTROL_FILE_ENV, &path);
        crate::control::set_remote_control_enabled(enabled).unwrap();
        let resp = f.await;
        std::env::remove_var(crate::control::CONTROL_FILE_ENV);
        let _ = std::fs::remove_file(&path);
        resp
    }

    #[tokio::test]
    async fn unknown_tool_is_unsupported() {
        let req = Request {
            id: "1".to_string(),
            tool: "does.not.exist".to_string(),
            args: json!({}),
        };
        let resp = handle(req).await;
        assert!(!resp.ok);
        assert_eq!(resp.error.unwrap().code, ErrorCode::Unsupported);
    }

    #[tokio::test]
    async fn powershell_echo_round_trips() {
        // powershell_exec is mutating, so this also exercises the "enabled" gate path.
        let resp = with_remote_control(true, "kenny-dispatch-ps-on.control.json", async {
            handle(Request {
                id: "2".to_string(),
                tool: "powershell_exec".to_string(),
                args: json!({"script": "printf hi"}),
            })
            .await
        })
        .await;
        assert!(resp.ok, "expected ok, got {:?}", resp.error);
        assert_eq!(resp.result.unwrap()["stdout"], "hi");
    }

    #[tokio::test]
    async fn telemetry_collect_returns_sections() {
        let req = Request {
            id: "3".to_string(),
            tool: "telemetry_collect".to_string(),
            args: json!({"sections": ["disk"]}),
        };
        let resp = handle(req).await;
        assert!(resp.ok);
        let result = resp.result.unwrap();
        assert!(result["disk"]["status"].is_string());
        assert!(result.get("memory").is_none());
    }

    #[tokio::test]
    async fn mutating_tool_blocked_when_disabled() {
        let resp = with_remote_control(false, "kenny-dispatch-ps-off.control.json", async {
            handle(Request {
                id: "4".to_string(),
                tool: "powershell_exec".to_string(),
                args: json!({"script": "printf hi"}),
            })
            .await
        })
        .await;
        assert!(!resp.ok, "mutating tool must be refused while disabled");
        assert_eq!(resp.error.unwrap().code, ErrorCode::Disabled);
    }

    #[tokio::test]
    async fn dangerous_powershell_is_blocked_even_when_enabled() {
        // The safety guard refuses a dangerous script independently of the kill-switch:
        // remote control is ON here, yet the call is still blocked.
        let resp = with_remote_control(true, "kenny-dispatch-ps-blocked.control.json", async {
            handle(Request {
                id: "6".to_string(),
                tool: "powershell_exec".to_string(),
                args: json!({"script": "vssadmin delete shadows /all /quiet"}),
            })
            .await
        })
        .await;
        assert!(!resp.ok, "dangerous script must be refused");
        assert_eq!(resp.error.unwrap().code, ErrorCode::Blocked);
    }

    #[tokio::test]
    async fn webfilter_apply_blocked_when_disabled() {
        // webfilter_apply is mutating, so the kill switch refuses it with `disabled`
        // before it ever reaches the (unsupported off-Windows) handler.
        let resp = with_remote_control(false, "kenny-dispatch-wf-off.control.json", async {
            handle(Request {
                id: "7".to_string(),
                tool: "webfilter_apply".to_string(),
                args: json!({
                    "domains": ["x.example"],
                    "doh_policy": "disable",
                    "list_hash": "deadbeefdeadbeef"
                }),
            })
            .await
        })
        .await;
        assert!(!resp.ok);
        assert_eq!(resp.error.unwrap().code, ErrorCode::Disabled);
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn webfilter_apply_unsupported_off_windows_when_enabled() {
        // With remote control ON, the mutating gate passes and the handler reports
        // `unsupported` on a non-Windows build.
        let resp = with_remote_control(true, "kenny-dispatch-wf-on.control.json", async {
            handle(Request {
                id: "8".to_string(),
                tool: "webfilter_apply".to_string(),
                args: json!({
                    "domains": ["x.example"],
                    "doh_policy": "disable",
                    "list_hash": "deadbeefdeadbeef"
                }),
            })
            .await
        })
        .await;
        assert!(!resp.ok);
        assert_eq!(resp.error.unwrap().code, ErrorCode::Unsupported);
    }

    #[tokio::test]
    async fn webfilter_status_allowed_when_disabled() {
        // Read-only status must work under the kill switch (and off Windows).
        let resp = with_remote_control(false, "kenny-dispatch-wf-status.control.json", async {
            handle(Request {
                id: "9".to_string(),
                tool: "webfilter_status".to_string(),
                args: json!({}),
            })
            .await
        })
        .await;
        assert!(resp.ok, "status is read-only and must work while disabled");
        assert!(resp.result.unwrap()["doh_policy"].is_object());
    }

    #[allow(clippy::await_holding_lock)] // single-threaded test runtime; lock guards global state
    #[tokio::test]
    async fn screen_capture_paused_while_protected_game_runs() {
        // Force the coexistence flag on (as if an anti-cheat process were running); the
        // gate must refuse `screen_capture` with `paused` before reaching the handler
        // (which would otherwise return `unsupported` off Windows). Serialized via the
        // shared env lock so the forced global never races other tests.
        let _g = crate::control::TEST_ENV_LOCK.lock().unwrap();
        crate::coexist::force_active_for_test(true, Some("EasyAntiCheat.exe"));
        let resp = handle(Request {
            id: "10".to_string(),
            tool: "screen_capture".to_string(),
            args: json!({}),
        })
        .await;
        crate::coexist::force_active_for_test(false, None);
        assert!(
            !resp.ok,
            "screen_capture must be refused while a game is active"
        );
        assert_eq!(resp.error.unwrap().code, ErrorCode::Paused);
    }

    #[tokio::test]
    async fn telemetry_allowed_when_disabled() {
        // Read-only/telemetry paths keep working even with remote control off.
        let resp = with_remote_control(false, "kenny-dispatch-tel-off.control.json", async {
            handle(Request {
                id: "5".to_string(),
                tool: "telemetry_collect".to_string(),
                args: json!({"sections": ["disk"]}),
            })
            .await
        })
        .await;
        assert!(resp.ok, "telemetry must keep working while disabled");
        assert!(resp.result.unwrap()["disk"]["status"].is_string());
    }
}
