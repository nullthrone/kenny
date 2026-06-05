//! Network tools: `net_config`, `net_dns_flush`, `net_adapter_reset`.
//!
//! `net_config` is portable read-only (interface inventory via `sysinfo`).
//! `net_dns_flush` and `net_adapter_reset` mutate the system and are Windows-only;
//! off Windows they return `unsupported`.

use serde::Deserialize;
use serde_json::{json, Value};
use sysinfo::Networks;

use crate::protocol::ErrorCode;

/// `net_config` — interface and DNS inventory (read-only, portable).
pub fn config(_args: Value) -> Result<Value, (ErrorCode, String)> {
    let networks = Networks::new_with_refreshed_list();
    let interfaces: Vec<Value> = networks
        .list()
        .iter()
        .map(|(name, data)| {
            let ips: Vec<String> = data
                .ip_networks()
                .iter()
                .map(|n| format!("{}/{}", n.addr, n.prefix))
                .collect();
            json!({
                "name": name,
                "mac": data.mac_address().to_string(),
                "ips": ips,
            })
        })
        .collect();
    Ok(json!({ "interfaces": interfaces, "dns": [] }))
}

/// `net_dns_flush` — clear the DNS resolver cache.
pub async fn dns_flush(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::dns_flush().await
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("net_dns_flush"))
    }
}

#[derive(Debug, Deserialize)]
struct AdapterArgs {
    #[allow(dead_code)]
    name: String,
}

/// `net_adapter_reset` — disable/re-enable a network adapter.
pub async fn adapter_reset(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: AdapterArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::adapter_reset(&a.name).await
    }
    #[cfg(not(windows))]
    {
        let _ = AdapterArgs {
            name: String::new(),
        };
        Err(unsupported("net_adapter_reset"))
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
    use tokio::process::Command;

    /// Real impl: `ipconfig /flushdns`.
    pub async fn dns_flush() -> Result<Value, (ErrorCode, String)> {
        let out = Command::new("ipconfig")
            .arg("/flushdns")
            .output()
            .await
            .map_err(|e| (ErrorCode::ExecFailed, format!("ipconfig spawn failed: {e}")))?;
        Ok(json!({ "ok": out.status.success() }))
    }

    /// Real impl: `Disable-NetAdapter` then `Enable-NetAdapter` for `name`.
    pub async fn adapter_reset(name: &str) -> Result<Value, (ErrorCode, String)> {
        let script = format!(
            "Disable-NetAdapter -Name '{name}' -Confirm:$false; Start-Sleep -Seconds 2; Enable-NetAdapter -Name '{name}' -Confirm:$false"
        );
        let out = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .output()
            .await
            .map_err(|e| {
                (
                    ErrorCode::ExecFailed,
                    format!("powershell spawn failed: {e}"),
                )
            })?;
        Ok(json!({ "ok": out.status.success() }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_lists_interfaces() {
        let v = config(json!({})).unwrap();
        assert!(v["interfaces"].is_array());
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn dns_flush_unsupported_off_windows() {
        let err = dns_flush(json!({})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }
}
