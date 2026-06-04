//! Tool dispatch: map a `request` frame to a handler and build a `response`.
//!
//! Unknown tools return `error.code = "unsupported"`. Handlers signal failure with
//! `(ErrorCode, message)`, which becomes `response.error`.

use serde_json::{json, Value};
use tracing::debug;

use crate::handlers;
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
    match tool {
        "powershell.exec" => handlers::powershell::exec(args).await,

        "fs.list" => handlers::fs::list(args),
        "fs.search" => handlers::fs::search(args),
        "fs.read" => handlers::fs::read(args),
        "fs.disk_usage" => handlers::fs::disk_usage(args),

        "winget.list" => handlers::winget::list(args).await,
        "winget.install" => handlers::winget::install(args).await,
        "winget.uninstall" => handlers::winget::uninstall(args).await,
        "winget.update" => handlers::winget::update(args).await,

        "diag.processes" => handlers::diagnostics::processes(args),
        "diag.services" => handlers::diagnostics::services(args),
        "diag.eventlog" => handlers::diagnostics::eventlog(args),
        "diag.autostart" => handlers::diagnostics::autostart(args),

        "net.config" => handlers::network::config(args),
        "net.dns_flush" => handlers::network::dns_flush(args).await,
        "net.adapter_reset" => handlers::network::adapter_reset(args).await,

        "screen.capture" => handlers::screenshot::capture(args),

        "telemetry.collect" => telemetry_collect(args),

        other => Err((ErrorCode::Unsupported, format!("unknown tool: {other}"))),
    }
}

/// `telemetry.collect` — return the snapshot map (optionally a subset of sections).
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
        let req = Request {
            id: "2".to_string(),
            tool: "powershell.exec".to_string(),
            args: json!({"script": "printf hi"}),
        };
        let resp = handle(req).await;
        assert!(resp.ok, "expected ok, got {:?}", resp.error);
        assert_eq!(resp.result.unwrap()["stdout"], "hi");
    }

    #[tokio::test]
    async fn telemetry_collect_returns_sections() {
        let req = Request {
            id: "3".to_string(),
            tool: "telemetry.collect".to_string(),
            args: json!({"sections": ["disk"]}),
        };
        let resp = handle(req).await;
        assert!(resp.ok);
        let result = resp.result.unwrap();
        assert!(result["disk"]["status"].is_string());
        assert!(result.get("memory").is_none());
    }
}
