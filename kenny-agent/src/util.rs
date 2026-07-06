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

/// Normalized CPU architecture for `register.meta.arch` (`x86_64`/`aarch64`).
///
/// Mirrors the server's `_norm_arch`: `aarch64`/`arm64` -> `aarch64`, else `x86_64`.
pub fn arch() -> &'static str {
    norm_arch(std::env::consts::ARCH)
}

/// Pure normalization, split out from [`arch`] so it's testable independent of the
/// host running the test suite.
fn norm_arch(raw: &str) -> &'static str {
    match raw {
        "aarch64" | "arm64" => "aarch64",
        _ => "x86_64",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn norm_arch_maps_arm_variants_to_aarch64() {
        assert_eq!(norm_arch("aarch64"), "aarch64");
        assert_eq!(norm_arch("arm64"), "aarch64");
    }

    #[test]
    fn norm_arch_defaults_everything_else_to_x86_64() {
        assert_eq!(norm_arch("x86_64"), "x86_64");
        assert_eq!(norm_arch("x86"), "x86_64");
        assert_eq!(norm_arch("arm"), "x86_64");
        assert_eq!(norm_arch(""), "x86_64");
    }

    #[test]
    fn arch_returns_one_of_the_two_supported_values() {
        assert!(matches!(arch(), "x86_64" | "aarch64"));
    }
}
