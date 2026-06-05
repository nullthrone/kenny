//! `winget_*` tools. Real implementation is Windows-only; off Windows these return
//! `unsupported` per the platform rule.

use serde_json::{json, Value};

use crate::protocol::ErrorCode;

/// `winget_list` — installed packages with available upgrades.
pub async fn list(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::list().await
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("winget_list"))
    }
}

#[derive(serde::Deserialize)]
struct IdArg {
    // Read only on Windows; constructed in the non-Windows stub for arg validation.
    #[allow(dead_code)]
    id: String,
}

#[derive(serde::Deserialize)]
struct OptIdArg {
    #[serde(default)]
    #[allow(dead_code)]
    id: Option<String>,
}

/// `winget_install` — install a package by id.
pub async fn install(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: IdArg =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::run_change(&[
            "install",
            "--id",
            &a.id,
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ])
        .await
    }
    #[cfg(not(windows))]
    {
        // Validate args even on the stub so bad calls are caught early.
        let _ = IdArg { id: String::new() };
        Err(unsupported("winget_install"))
    }
}

/// `winget_uninstall` — uninstall a package by id.
pub async fn uninstall(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: IdArg =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::run_change(&["uninstall", "--id", &a.id, "--silent"]).await
    }
    #[cfg(not(windows))]
    {
        let _ = IdArg { id: String::new() };
        Err(unsupported("winget_uninstall"))
    }
}

/// `winget_update` — upgrade one package (`id`) or all packages when omitted.
pub async fn update(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: OptIdArg =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        let mut args = vec![
            "upgrade",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ];
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
        Err(unsupported("winget_update"))
    }
}

#[cfg(not(windows))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

/// Parse winget's fixed-width table (`winget list` / `winget upgrade`) into
/// `{id, name, version, available}` rows. Columns are located by the header line
/// (`Name  Id  Version  [Available]  [Source]`); rows below the `---` divider are
/// sliced by those offsets. The `Available` column is optional — `winget list`
/// omits it when no upgrades are pending — in which case `available` is `""`.
///
/// Platform-neutral so it can be unit-tested on non-Windows CI; shared by the
/// `winget_list` handler and the `app_updates` telemetry collector. Off Windows it
/// is only reached from tests, so the bin build would otherwise flag it dead.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn parse_table(raw: &str) -> Vec<Value> {
    let lines: Vec<&str> = raw.lines().collect();
    // Find the header row containing "Name", "Id" and "Version".
    let header_idx = lines.iter().position(|l| {
        let t = l.trim_start();
        t.starts_with("Name") && l.contains("Id") && l.contains("Version")
    });
    let Some(hidx) = header_idx else {
        return Vec::new();
    };
    let header = lines[hidx];
    // Column start offsets by header keyword. Id + Version are required; Available
    // and Source are optional (absent in `winget list` with no pending upgrades).
    let (Some(id_col), Some(ver_col)) = (header.find("Id"), header.find("Version")) else {
        return Vec::new();
    };
    let avail_col = header.find("Available");
    let src_col = header.find("Source");
    // The version column ends at whichever of Available / Source / end-of-line comes first.
    let ver_end = avail_col.or(src_col);

    let mut packages = Vec::new();
    for line in lines.iter().skip(hidx + 1) {
        // Skip the divider and any trailing "N upgrades available" footer.
        if line.trim().is_empty() || line.trim_start().starts_with('-') {
            continue;
        }
        // Footer/short lines have no column structure: require the line reach the
        // version column.
        if line.chars().count() < ver_col {
            continue;
        }
        let name = slice_cols(line, 0, id_col).trim().to_string();
        let id = slice_cols(line, id_col, ver_col).trim().to_string();
        let version = match ver_end {
            Some(end) => slice_cols(line, ver_col, end).trim().to_string(),
            None => line
                .chars()
                .skip(ver_col)
                .collect::<String>()
                .trim()
                .to_string(),
        };
        let available = match avail_col {
            Some(a) => match src_col {
                Some(s) => slice_cols(line, a, s).trim().to_string(),
                None => line.chars().skip(a).collect::<String>().trim().to_string(),
            },
            None => String::new(),
        };
        // A real winget Id never contains whitespace; free-text footers ("N
        // packages available …") slice into an Id cell with spaces, so reject those.
        if id.is_empty() || name.is_empty() || id.split_whitespace().count() != 1 {
            continue;
        }
        packages.push(json!({
            "id": id,
            "name": name,
            "version": version,
            "available": available,
        }));
    }
    packages
}

/// Slice a line by character offsets `[start, end)`, tolerating short lines and
/// multibyte characters (winget pads with Unicode where locales vary).
fn slice_cols(line: &str, start: usize, end: usize) -> String {
    line.chars()
        .skip(start)
        .take(end.saturating_sub(start))
        .collect()
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
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

    /// `winget_list` real implementation: run `winget list` and parse its
    /// fixed-width table into `{id,name,version,available}` rows.
    pub async fn list() -> Result<Value, (ErrorCode, String)> {
        let output = Command::new("winget")
            .args(["list", "--accept-source-agreements"])
            .output()
            .await
            .map_err(|e| (ErrorCode::ExecFailed, format!("winget spawn failed: {e}")))?;
        let raw = String::from_utf8_lossy(&output.stdout);
        Ok(json!({ "packages": super::parse_table(&raw) }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // `winget list` with pending upgrades: full header incl. Available + Source.
    const LIST_WITH_AVAILABLE: &str = "\
Name                 Id                     Version      Available    Source
-------------------------------------------------------------------------------
Mozilla Firefox      Mozilla.Firefox        118.0.1      119.0        winget
7-Zip                7zip.7zip              22.01                     winget
3 packages available from a configured source.
";

    // `winget list` with no upgrades pending: winget omits the Available column.
    const LIST_NO_AVAILABLE: &str = "\
Name                 Id                     Version      Source
------------------------------------------------------------------
Mozilla Firefox      Mozilla.Firefox        119.0        winget
7-Zip                7zip.7zip              22.01         winget
";

    #[test]
    fn parses_available_column_when_present() {
        let pkgs = parse_table(LIST_WITH_AVAILABLE);
        assert_eq!(pkgs.len(), 2);
        assert_eq!(pkgs[0]["id"], "Mozilla.Firefox");
        assert_eq!(pkgs[0]["name"], "Mozilla Firefox");
        assert_eq!(pkgs[0]["version"], "118.0.1");
        assert_eq!(pkgs[0]["available"], "119.0");
        // Row without a pending upgrade has an empty Available cell.
        assert_eq!(pkgs[1]["id"], "7zip.7zip");
        assert_eq!(pkgs[1]["version"], "22.01");
        assert_eq!(pkgs[1]["available"], "");
    }

    #[test]
    fn tolerates_missing_available_column() {
        let pkgs = parse_table(LIST_NO_AVAILABLE);
        assert_eq!(pkgs.len(), 2);
        assert_eq!(pkgs[0]["id"], "Mozilla.Firefox");
        assert_eq!(pkgs[0]["version"], "119.0");
        assert_eq!(pkgs[0]["available"], "");
        assert_eq!(pkgs[1]["id"], "7zip.7zip");
        assert_eq!(pkgs[1]["available"], "");
    }

    #[test]
    fn skips_divider_and_footer_lines() {
        // No "Name/Id/Version" header at all → nothing to parse.
        assert!(parse_table("garbage output\nwith no table\n").is_empty());
        // The "N packages available" footer must not become a package row.
        let pkgs = parse_table(LIST_WITH_AVAILABLE);
        assert!(pkgs.iter().all(|p| p["id"] != ""));
    }

    #[test]
    fn handles_unicode_padded_columns() {
        // Localised winget pads with multibyte characters; column offsets are by
        // char, so a non-ASCII name must not shift the parsed fields.
        let raw = "\
Name                 Id                     Version      Available    Source
-------------------------------------------------------------------------------
Mödìfïér Pro         Vendor.Modifier        1.2.3        1.2.4        winget
";
        let pkgs = parse_table(raw);
        assert_eq!(pkgs.len(), 1);
        assert_eq!(pkgs[0]["id"], "Vendor.Modifier");
        assert_eq!(pkgs[0]["name"], "Mödìfïér Pro");
        assert_eq!(pkgs[0]["version"], "1.2.3");
        assert_eq!(pkgs[0]["available"], "1.2.4");
    }
}
