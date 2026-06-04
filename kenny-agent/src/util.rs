//! Small shared helpers.

/// Current time as an RFC 3339 / ISO 8601 string in UTC (`...Z`).
pub fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Detected OS family string for `register.meta.os` (`windows`/`linux`/`macos`).
pub fn os_family() -> &'static str {
    if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    }
}

/// Best-effort hostname for `register.meta.hostname`.
pub fn hostname() -> String {
    sysinfo::System::host_name().unwrap_or_else(|| "unknown".to_string())
}
