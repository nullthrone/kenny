//! `powershell_exec` — run a script and return stdout/stderr/exit_code.
//!
//! On Windows this shells out to `powershell.exe -NoProfile -Command <script>`.
//! Off Windows (dev/CI) it falls back to `sh -c <script>` so e2e flows work.

use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::process::Command;

use crate::protocol::ErrorCode;

#[derive(Debug, Deserialize)]
struct Args {
    script: String,
    #[serde(default)]
    timeout_s: Option<u64>,
}

/// Execute the requested script, honouring `timeout_s` if present.
pub async fn exec(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: Args = serde_json::from_value(args).map_err(|e| {
        (
            ErrorCode::BadArgs,
            format!("invalid powershell_exec args: {e}"),
        )
    })?;

    let mut cmd = build_command(&args.script);
    let fut = cmd.output();

    let output = match args.timeout_s {
        Some(secs) => match tokio::time::timeout(Duration::from_secs(secs), fut).await {
            Ok(res) => res,
            Err(_) => return Err((ErrorCode::Timeout, format!("tool exceeded {secs}s"))),
        },
        None => fut.await,
    }
    .map_err(|e| (ErrorCode::ExecFailed, format!("failed to spawn shell: {e}")))?;

    Ok(json!({
        "stdout": String::from_utf8_lossy(&output.stdout),
        "stderr": String::from_utf8_lossy(&output.stderr),
        "exit_code": output.status.code().unwrap_or(-1),
    }))
}

#[cfg(windows)]
fn build_command(script: &str) -> Command {
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", script]);
    cmd
}

#[cfg(not(windows))]
fn build_command(script: &str) -> Command {
    let mut cmd = Command::new("sh");
    cmd.args(["-c", script]);
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn echoes_stdout_via_fallback() {
        // On non-Windows CI this runs `sh -c 'printf hi'`.
        let result = exec(json!({"script": "printf hi"})).await.unwrap();
        assert_eq!(result["stdout"], "hi");
        assert_eq!(result["exit_code"], 0);
    }

    #[tokio::test]
    async fn rejects_bad_args() {
        let err = exec(json!({"nope": 1})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[tokio::test]
    async fn times_out() {
        let err = exec(json!({"script": "sleep 5", "timeout_s": 1}))
            .await
            .unwrap_err();
        assert_eq!(err.0, ErrorCode::Timeout);
    }
}
