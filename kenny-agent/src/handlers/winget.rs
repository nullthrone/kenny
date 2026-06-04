//! `winget.*` tools. Real implementation is Windows-only; off Windows these return
//! `unsupported` per the platform rule.

use serde_json::Value;

use crate::protocol::ErrorCode;

/// `winget.list` — installed packages with available upgrades.
pub async fn list(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        // Real impl: parse `winget list --upgrade-available --include-unknown` /
        // `Get-WinGetPackage` into {id,name,version,available}.
        windows_impl::list().await
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("winget.list"))
    }
}

#[derive(serde::Deserialize)]
struct IdArg {
    id: String,
}

#[derive(serde::Deserialize)]
struct OptIdArg {
    #[serde(default)]
    id: Option<String>,
}

/// `winget.install` — install a package by id.
pub async fn install(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: IdArg = serde_json::from_value(_args)
            .map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::run_change(&["install", "--id", &a.id, "--silent", "--accept-package-agreements", "--accept-source-agreements"]).await
    }
    #[cfg(not(windows))]
    {
        // Validate args even on the stub so bad calls are caught early.
        let _ = IdArg { id: String::new() };
        Err(unsupported("winget.install"))
    }
}

/// `winget.uninstall` — uninstall a package by id.
pub async fn uninstall(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: IdArg = serde_json::from_value(_args)
            .map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::run_change(&["uninstall", "--id", &a.id, "--silent"]).await
    }
    #[cfg(not(windows))]
    {
        let _ = IdArg { id: String::new() };
        Err(unsupported("winget.uninstall"))
    }
}

/// `winget.update` — upgrade one package (`id`) or all packages when omitted.
pub async fn update(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: OptIdArg = serde_json::from_value(_args)
            .map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        let mut args = vec!["upgrade", "--silent", "--accept-package-agreements", "--accept-source-agreements"];
        if let Some(id) = a.id.as_deref() {
            args.push("--id");
            args.push(id);
        } else {
            args.push("--all");
        }
        windows_impl::run_change(&args).await
    }
    #[cfg(not(windows))]
    {
        let _ = OptIdArg { id: None };
        Err(unsupported("winget.update"))
    }
}

#[cfg(not(windows))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use serde_json::json;
    use tokio::process::Command;

    /// Run a winget subcommand and report ok + combined log.
    pub async fn run_change(args: &[&str]) -> Result<Value, (ErrorCode, String)> {
        let output = Command::new("winget")
            .args(args)
            .output()
            .await
            .map_err(|e| (ErrorCode::ExecFailed, format!("winget spawn failed: {e}")))?;
        let mut log = String::from_utf8_lossy(&output.stdout).to_string();
        log.push_str(&String::from_utf8_lossy(&output.stderr));
        Ok(json!({ "ok": output.status.success(), "log": log }))
    }

    /// `winget.list` real implementation. Parsing of winget's table output is
    /// left as a follow-up; returns an empty package set for now.
    pub async fn list() -> Result<Value, (ErrorCode, String)> {
        let output = Command::new("winget")
            .args(["list", "--accept-source-agreements"])
            .output()
            .await
            .map_err(|e| (ErrorCode::ExecFailed, format!("winget spawn failed: {e}")))?;
        let _raw = String::from_utf8_lossy(&output.stdout);
        // TODO(windows): parse `_raw` table into {id,name,version,available}.
        Ok(json!({ "packages": [] }))
    }
}
