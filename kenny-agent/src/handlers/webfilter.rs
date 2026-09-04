//! `webfilter_status|apply|clear` — parental-controls web filtering (ADR-0024).
//!
//! The agent is a **dumb, idempotent enforcer**: the server owns the per-host list and
//! sends the full flat `domains` block set. The agent writes it as a marker-delimited
//! block in the OS hosts file (atomic replace), optionally disables browser DNS-over-HTTPS
//! via registry policy so DoH cannot bypass the block, and flushes DNS. It refuses
//! (`blocked`) any list that would blackhole a self-protected name so a bad list can never
//! sever the tunnel or OS updates.
//!
//! The hosts-splicing/validation/hashing **core** is `#[cfg(windows)]`-free and unit-tested
//! on Linux CI (driven through `KENNY_HOSTS_FILE`); the real hosts path, registry writes,
//! and DNS flush are Windows-gated. Off Windows `apply`/`clear` return `unsupported` and
//! `status` reports `{active:false, supported:false, ...}` (read-only must work on dev builds).

use std::path::PathBuf;

use serde_json::{json, Value};

use crate::protocol::ErrorCode;

/// Environment override for the hosts-file path (test hook, mirrors
/// `KENNY_CONTROL_FILE`). When unset, [`hosts_path`] uses the OS default.
pub const HOSTS_FILE_ENV: &str = "KENNY_HOSTS_FILE";

// The hosts-splicing/hashing core below is consumed by the Windows impl and by the
// portable unit tests (run on every platform). In a non-test Linux `cargo build` those
// consumers are absent, so silence the resulting dead-code warnings there — the same
// pattern `network.rs` uses for `ps_single_quote`.

/// Opening marker of kenny's managed block in the hosts file.
#[cfg_attr(not(windows), allow(dead_code))]
const BEGIN: &str = "# kenny-webfilter begin (managed by kenny — do not edit inside this block)";
/// Closing marker of kenny's managed block.
#[cfg_attr(not(windows), allow(dead_code))]
const END: &str = "# kenny-webfilter end";

/// Hard cap on the block list (contract): above this the agent returns `bad_args` rather
/// than silently truncating, so a server/agent cap mismatch surfaces.
const MAX_DOMAINS: usize = 10_000;

/// `webfilter_status` — READ-ONLY; works under the kill switch and off Windows.
pub async fn status(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::status().await
    }
    #[cfg(not(windows))]
    {
        Ok(json!({
            "active": false,
            "supported": false,
            "entry_count": 0,
            "list_hash": "",
            "doh_policy": { "chrome": "unknown", "edge": "unknown", "firefox": "unknown" },
            "applied_at": null,
        }))
    }
}

/// Arguments for `webfilter_apply`.
#[derive(serde::Deserialize)]
struct ApplyArgs {
    domains: Vec<String>,
    doh_policy: String,
    #[allow(dead_code)] // echoed back after recompute; the recomputed hash is authoritative
    list_hash: String,
}

/// `webfilter_apply` — MUTATING. Writes the block set to the hosts file, optionally
/// disables browser DoH, flushes DNS. Off Windows: `unsupported`.
pub async fn apply(_args: Value) -> Result<Value, (ErrorCode, String)> {
    // Parse + validate on every platform so malformed/dangerous calls are caught early
    // and consistently (a reserved-domain block is `blocked` even on a dev build).
    let args: ApplyArgs =
        serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
    if args.doh_policy != "disable" && args.doh_policy != "leave" {
        return Err((
            ErrorCode::BadArgs,
            format!(
                "doh_policy must be \"disable\" or \"leave\", got {:?}",
                args.doh_policy
            ),
        ));
    }
    let domains = validate_domains(&args.domains)?;

    #[cfg(windows)]
    {
        windows_impl::apply(domains, &args.doh_policy).await
    }
    #[cfg(not(windows))]
    {
        let _ = domains;
        Err(unsupported("webfilter_apply"))
    }
}

/// `webfilter_clear` — MUTATING. Removes kenny's block + kenny-written DoH policy values,
/// flushes DNS. Off Windows: `unsupported`.
pub async fn clear(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::clear().await
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("webfilter_clear"))
    }
}

#[cfg(not(windows))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

/// Resolve the hosts-file path: `KENNY_HOSTS_FILE` override → the Windows default.
#[cfg_attr(not(windows), allow(dead_code))]
fn hosts_path() -> PathBuf {
    if let Some(p) = std::env::var_os(HOSTS_FILE_ENV) {
        return PathBuf::from(p);
    }
    PathBuf::from(r"C:\Windows\System32\drivers\etc\hosts")
}

/// Render kenny's managed block: BEGIN marker, one `0.0.0.0 <domain>` line each, END.
#[cfg_attr(not(windows), allow(dead_code))]
fn render_block(domains: &[String]) -> String {
    let mut s = String::new();
    s.push_str(BEGIN);
    s.push('\n');
    for d in domains {
        s.push_str("0.0.0.0 ");
        s.push_str(d);
        s.push('\n');
    }
    s.push_str(END);
    s.push('\n');
    s
}

/// Splice kenny's block into `existing` hosts-file content.
///
/// Removes any existing BEGIN..END region (inclusive, plus the blank line that separated
/// it), then — if `block` is `Some` — appends it (separated by a blank line when the file
/// is non-empty). IDEMPOTENT: applying the same block twice yields byte-identical output;
/// content outside the markers is preserved verbatim.
#[cfg_attr(not(windows), allow(dead_code))]
fn splice_block(existing: &str, block: Option<&str>) -> String {
    let without = strip_block(existing);
    match block {
        None => without,
        Some(block) => {
            if without.is_empty() {
                block.to_string()
            } else {
                // Exactly one blank line between prior content and our block.
                let base = without.trim_end_matches('\n');
                format!("{base}\n\n{block}")
            }
        }
    }
}

/// Remove kenny's BEGIN..END region (inclusive) from `content`, returning the remainder
/// with trailing blank lines it introduced trimmed to a single trailing newline (or empty).
#[cfg_attr(not(windows), allow(dead_code))]
fn strip_block(content: &str) -> String {
    let Some(begin) = content.find(BEGIN) else {
        return content.to_string();
    };
    // Find the END marker after BEGIN and consume through its line's newline.
    let after_begin = &content[begin..];
    let Some(end_rel) = after_begin.find(END) else {
        // Malformed (BEGIN without END): drop from BEGIN to end of file to be safe.
        let head = content[..begin].trim_end_matches('\n');
        return normalize_tail(head);
    };
    let end_abs = begin + end_rel + END.len();
    // Consume the rest of the END line (up to and including its newline).
    let mut tail_start = end_abs;
    let bytes = content.as_bytes();
    while tail_start < bytes.len() && bytes[tail_start] != b'\n' {
        tail_start += 1;
    }
    if tail_start < bytes.len() {
        tail_start += 1; // include the newline
    }
    let head = &content[..begin];
    let tail = &content[tail_start..];
    // Rejoin head + tail, collapsing the blank separator our block introduced.
    let head = head.trim_end_matches('\n');
    let tail = tail.trim_start_matches('\n');
    let joined = if head.is_empty() {
        tail.to_string()
    } else if tail.is_empty() {
        head.to_string()
    } else {
        format!("{head}\n{tail}")
    };
    normalize_tail(&joined)
}

/// Ensure non-empty content ends with exactly one newline; empty stays empty.
#[cfg_attr(not(windows), allow(dead_code))]
fn normalize_tail(s: &str) -> String {
    let trimmed = s.trim_end_matches('\n');
    if trimmed.is_empty() {
        String::new()
    } else {
        format!("{trimmed}\n")
    }
}

/// Count the `0.0.0.0 <domain>` entries in kenny's block within `content`, if present.
#[cfg_attr(not(windows), allow(dead_code))]
fn block_entry_count(content: &str) -> Option<usize> {
    let begin = content.find(BEGIN)?;
    let after = &content[begin..];
    let end_rel = after.find(END)?;
    let block = &after[..end_rel];
    Some(
        block
            .lines()
            .filter(|l| l.trim_start().starts_with("0.0.0.0 "))
            .count(),
    )
}

/// Extract the block's domains (for recomputing `list_hash` in `status`).
#[cfg_attr(not(windows), allow(dead_code))]
fn block_domains(content: &str) -> Vec<String> {
    let Some(begin) = content.find(BEGIN) else {
        return Vec::new();
    };
    let after = &content[begin..];
    let Some(end_rel) = after.find(END) else {
        return Vec::new();
    };
    after[..end_rel]
        .lines()
        .filter_map(|l| l.trim().strip_prefix("0.0.0.0 "))
        .map(|d| d.trim().to_string())
        .filter(|d| !d.is_empty())
        .collect()
}

/// `sha256(sorted-unique domains joined by '\n')`, lowercase hex, first 16 chars — the
/// same shape the server computes, for drift detection.
#[cfg_attr(not(windows), allow(dead_code))]
fn list_hash(domains: &[String]) -> String {
    use sha2::{Digest, Sha256};
    let mut sorted: Vec<String> = domains.to_vec();
    sorted.sort();
    sorted.dedup();
    let joined = sorted.join("\n");
    let digest = Sha256::digest(joined.as_bytes());
    let mut hex = String::with_capacity(32);
    for b in digest.iter().take(8) {
        use std::fmt::Write;
        let _ = write!(hex, "{b:02x}");
    }
    hex
}

/// Normalize + validate the pushed block list.
///
/// Lowercases/trims each entry, rejects empty/oversized/ill-formed names, enforces the
/// [`MAX_DOMAINS`] cap (`bad_args` above it), and refuses (`blocked`) any entry in the
/// self-protection reserved set so a bad list can never blackhole the tunnel or OS updates.
/// Returns the sorted, de-duplicated normalized list.
fn validate_domains(domains: &[String]) -> Result<Vec<String>, (ErrorCode, String)> {
    if domains.len() > MAX_DOMAINS {
        return Err((
            ErrorCode::BadArgs,
            format!(
                "domain list exceeds hard cap of {MAX_DOMAINS} ({} supplied)",
                domains.len()
            ),
        ));
    }

    let mut out = Vec::with_capacity(domains.len());
    for raw in domains {
        let d = raw.trim().trim_end_matches('.').to_ascii_lowercase();
        if d.is_empty() {
            return Err((ErrorCode::BadArgs, "empty domain in list".to_string()));
        }
        if d.len() > 253 {
            return Err((
                ErrorCode::BadArgs,
                format!("domain too long ({} chars): {d}", d.len()),
            ));
        }
        // Hostname charset: letters, digits, dot, hyphen. Rejects whitespace and any
        // hosts-file metacharacter that could smuggle a second entry onto one line.
        if !d
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
        {
            return Err((
                ErrorCode::BadArgs,
                format!("domain has invalid characters: {d}"),
            ));
        }
        if is_reserved(&d) {
            return Err((
                ErrorCode::Blocked,
                format!("refused: {d} is a self-protected name and cannot be blocked"),
            ));
        }
        out.push(d);
    }
    out.sort();
    out.dedup();
    Ok(out)
}

/// The self-protection reserved set: names the agent must never blackhole. Blocking any of
/// these could sever the server tunnel or OS updates, so `apply` refuses the whole list.
///
/// - `localhost` / `localhost.localdomain` — loopback.
/// - the configured server host (from [`crate::policy::server_host`]) and its parent
///   labels — so the control channel can never be cut.
/// - core Microsoft-update infrastructure (`windowsupdate.com`, `update.microsoft.com`,
///   `microsoft.com`) — so OS updates keep flowing.
///
/// A name matches if it equals a reserved name or is a subdomain of one (suffix match),
/// mirroring how a hosts entry for `microsoft.com` would also be reached by the update
/// host resolving through it.
fn is_reserved(domain: &str) -> bool {
    const STATIC_RESERVED: &[&str] = &[
        "localhost",
        "localhost.localdomain",
        "windowsupdate.com",
        "update.microsoft.com",
        "microsoft.com",
    ];
    let matches = |reserved: &str| domain == reserved || domain.ends_with(&format!(".{reserved}"));

    if STATIC_RESERVED.iter().any(|r| matches(r)) {
        return true;
    }
    if let Some(server) = crate::policy::server_host() {
        let server = server.to_ascii_lowercase();
        if !server.is_empty() && matches(&server) {
            return true;
        }
    }
    false
}

/// Portable hosts-file mutation: splice `domains` into the file at `path` (atomic replace),
/// returning the number of entries written. Windows `apply` calls this then sets registry
/// policy; unit tests drive it directly via `KENNY_HOSTS_FILE`.
#[cfg_attr(not(windows), allow(dead_code))]
fn apply_hosts_only(
    path: &std::path::Path,
    domains: &[String],
) -> Result<usize, (ErrorCode, String)> {
    let existing = read_hosts(path)?;
    let block = render_block(domains);
    let next = splice_block(&existing, Some(&block));
    write_atomic(path, &next)?;
    Ok(domains.len())
}

/// Portable hosts-file clear: remove kenny's block from the file at `path`, returning the
/// number of entries that were removed.
#[cfg_attr(not(windows), allow(dead_code))]
fn clear_hosts_only(path: &std::path::Path) -> Result<usize, (ErrorCode, String)> {
    let existing = read_hosts(path)?;
    let removed = block_entry_count(&existing).unwrap_or(0);
    let next = splice_block(&existing, None);
    write_atomic(path, &next)?;
    Ok(removed)
}

/// Read the hosts file; a missing file reads as empty (first-ever apply).
#[cfg_attr(not(windows), allow(dead_code))]
fn read_hosts(path: &std::path::Path) -> Result<String, (ErrorCode, String)> {
    match std::fs::read_to_string(path) {
        Ok(s) => Ok(s),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err((
            ErrorCode::ExecFailed,
            format!("cannot read hosts file {}: {e}", path.display()),
        )),
    }
}

/// Write `content` to `path` atomically (temp file in the same dir + rename) so a reader
/// never sees a half-written hosts file.
#[cfg_attr(not(windows), allow(dead_code))]
fn write_atomic(path: &std::path::Path, content: &str) -> Result<(), (ErrorCode, String)> {
    let dir = path.parent().unwrap_or_else(|| std::path::Path::new("."));
    let tmp = dir.join(format!(
        ".kenny-hosts-{}-{}.tmp",
        std::process::id(),
        uuid::Uuid::new_v4()
    ));
    std::fs::write(&tmp, content).map_err(|e| {
        (
            ErrorCode::ExecFailed,
            format!("cannot write temp hosts file {}: {e}", tmp.display()),
        )
    })?;
    clear_readonly_if_present(path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })?;
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        (
            ErrorCode::ExecFailed,
            format!("cannot replace hosts file {}: {e}", path.display()),
        )
    })?;
    Ok(())
}

/// Clear the read-only attribute on `path` if it exists and is read-only.
///
/// On Windows, replacing an existing file that has `FILE_ATTRIBUTE_READONLY` set fails
/// with `ERROR_ACCESS_DENIED` even for SYSTEM — the attribute check happens independently
/// of the caller's privileges. Antivirus products and hardening guides commonly set the
/// hosts file read-only as an anti-hijack measure, so clear it before the atomic replace
/// rather than surface a misleading "access denied" on an otherwise-privileged process.
#[cfg_attr(not(windows), allow(dead_code))]
// On Windows this only clears FILE_ATTRIBUTE_READONLY, not an ACL; the "world writable on
// Unix" caveat clippy warns about doesn't bite here — the Unix path is test/dev-only (a real
// tool call is `unsupported` off Windows), never the production hosts file.
#[allow(clippy::permissions_set_readonly_false)]
fn clear_readonly_if_present(path: &std::path::Path) -> Result<(), (ErrorCode, String)> {
    match std::fs::metadata(path) {
        Ok(meta) => {
            let mut perms = meta.permissions();
            if perms.readonly() {
                perms.set_readonly(false);
                std::fs::set_permissions(path, perms).map_err(|e| {
                    (
                        ErrorCode::ExecFailed,
                        format!(
                            "cannot clear read-only attribute on hosts file {}: {e}",
                            path.display()
                        ),
                    )
                })?;
            }
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err((
            ErrorCode::ExecFailed,
            format!("cannot stat hosts file {}: {e}", path.display()),
        )),
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use tokio::process::Command;

    /// Registry policy keys/values kenny sets to disable browser DoH.
    const CHROME_KEY: &str = r"HKLM\SOFTWARE\Policies\Google\Chrome";
    const EDGE_KEY: &str = r"HKLM\SOFTWARE\Policies\Microsoft\Edge";
    const FIREFOX_KEY: &str = r"HKLM\SOFTWARE\Policies\Mozilla\Firefox\DNSOverHTTPS";

    pub async fn status() -> Result<Value, (ErrorCode, String)> {
        let path = super::hosts_path();
        let existing = super::read_hosts(&path)?;
        let domains = super::block_domains(&existing);
        let active = existing.contains(super::BEGIN);
        let entry_count = super::block_entry_count(&existing).unwrap_or(0);
        let hash = if domains.is_empty() {
            String::new()
        } else {
            super::list_hash(&domains)
        };
        let doh = read_doh_policy().await;
        Ok(json!({
            "active": active,
            "supported": true,
            "entry_count": entry_count,
            "list_hash": hash,
            "doh_policy": doh,
            // The agent does not persist when the block was applied; the server tracks it.
            "applied_at": null,
        }))
    }

    pub async fn apply(
        domains: Vec<String>,
        doh_policy: &str,
    ) -> Result<Value, (ErrorCode, String)> {
        let path = super::hosts_path();
        let applied = super::apply_hosts_only(&path, &domains)?;

        let doh_applied = if doh_policy == "disable" {
            set_doh_disabled().await;
            true
        } else {
            false
        };

        flush_dns().await;

        Ok(json!({
            "ok": true,
            "applied": applied,
            "doh_policy_applied": doh_applied,
            "list_hash": super::list_hash(&domains),
            "applied_at": crate::util::now_rfc3339(),
        }))
    }

    pub async fn clear() -> Result<Value, (ErrorCode, String)> {
        let path = super::hosts_path();
        let removed = super::clear_hosts_only(&path)?;
        let doh_cleared = clear_doh_policy().await;
        flush_dns().await;
        Ok(json!({
            "ok": true,
            "removed_entries": removed,
            "doh_policy_cleared": doh_cleared,
        }))
    }

    /// Report the current per-browser DoH policy as `off`/`on`/`unknown`.
    async fn read_doh_policy() -> Value {
        json!({
            "chrome": chromium_doh_state(CHROME_KEY).await,
            "edge": chromium_doh_state(EDGE_KEY).await,
            "firefox": firefox_doh_state().await,
        })
    }

    /// Chrome/Edge: `DnsOverHttpsMode` REG_SZ; `off` => disabled, present-and-other => on.
    async fn chromium_doh_state(key: &str) -> &'static str {
        match reg_query_value(key, "DnsOverHttpsMode").await {
            Some(v) if v.eq_ignore_ascii_case("off") => "off",
            Some(_) => "on",
            None => "unknown",
        }
    }

    /// Firefox: `Enabled` REG_DWORD under the DNSOverHTTPS policy key; `0` => off.
    async fn firefox_doh_state() -> &'static str {
        match reg_query_value(FIREFOX_KEY, "Enabled").await {
            Some(v) => {
                let v = v.trim();
                if v == "0x0" || v == "0" {
                    "off"
                } else {
                    "on"
                }
            }
            None => "unknown",
        }
    }

    /// Set the registry policies that turn browser DoH off. Best-effort per browser.
    async fn set_doh_disabled() {
        reg_add(CHROME_KEY, "DnsOverHttpsMode", "REG_SZ", "off").await;
        reg_add(EDGE_KEY, "DnsOverHttpsMode", "REG_SZ", "off").await;
        reg_add(FIREFOX_KEY, "Enabled", "REG_DWORD", "0").await;
        reg_add(FIREFOX_KEY, "Locked", "REG_DWORD", "1").await;
    }

    /// Remove only the values kenny wrote. Returns whether any deletion succeeded.
    async fn clear_doh_policy() -> bool {
        let mut any = false;
        any |= reg_delete_value(CHROME_KEY, "DnsOverHttpsMode").await;
        any |= reg_delete_value(EDGE_KEY, "DnsOverHttpsMode").await;
        any |= reg_delete_value(FIREFOX_KEY, "Enabled").await;
        any |= reg_delete_value(FIREFOX_KEY, "Locked").await;
        any
    }

    async fn reg_add(key: &str, name: &str, ty: &str, data: &str) {
        let _ = Command::new("reg")
            .args(["add", key, "/v", name, "/t", ty, "/d", data, "/f"])
            .output()
            .await;
    }

    async fn reg_delete_value(key: &str, name: &str) -> bool {
        Command::new("reg")
            .args(["delete", key, "/v", name, "/f"])
            .output()
            .await
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    /// Query a single registry value's data column via `reg query`. Returns the raw data
    /// token (e.g. `off`, `0x0`), or `None` when the key/value is absent.
    async fn reg_query_value(key: &str, name: &str) -> Option<String> {
        let out = Command::new("reg")
            .args(["query", key, "/v", name])
            .output()
            .await
            .ok()?;
        if !out.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&out.stdout);
        // Lines look like: "    DnsOverHttpsMode    REG_SZ    off".
        for line in text.lines() {
            if let Some(rest) = line.trim().strip_prefix(name) {
                return rest.split_whitespace().nth(1).map(str::to_string);
            }
        }
        None
    }

    async fn flush_dns() {
        let _ = Command::new("ipconfig").arg("/flushdns").output().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Vec<String> {
        vec![
            "badsite.example".to_string(),
            "www.badsite.example".to_string(),
        ]
    }

    #[test]
    fn render_block_shape() {
        let b = render_block(&sample());
        assert!(b.starts_with(BEGIN));
        assert!(b.contains("\n0.0.0.0 badsite.example\n"));
        assert!(b.contains("\n0.0.0.0 www.badsite.example\n"));
        assert!(b.trim_end().ends_with(END));
    }

    #[test]
    fn splice_is_idempotent() {
        let existing = "127.0.0.1 localhost\n::1 localhost\n";
        let block = render_block(&sample());
        let once = splice_block(existing, Some(&block));
        let twice = splice_block(&once, Some(&block));
        assert_eq!(
            once, twice,
            "applying the same block twice must be byte-identical"
        );
        // User content preserved.
        assert!(once.contains("127.0.0.1 localhost"));
        assert!(once.contains("::1 localhost"));
        // Exactly one managed block.
        assert_eq!(once.matches(BEGIN).count(), 1);
        assert_eq!(once.matches(END).count(), 1);
    }

    #[test]
    fn splice_preserves_lines_outside_markers() {
        let existing = "# top comment\n10.0.0.1 intra.example\n";
        let out = splice_block(existing, Some(&render_block(&sample())));
        assert!(out.contains("# top comment"));
        assert!(out.contains("10.0.0.1 intra.example"));
        // Re-applying a different block replaces only the managed region.
        let out2 = splice_block(&out, Some(&render_block(&["only.example".to_string()])));
        assert!(out2.contains("# top comment"));
        assert!(out2.contains("10.0.0.1 intra.example"));
        assert!(out2.contains("0.0.0.0 only.example"));
        assert!(!out2.contains("0.0.0.0 badsite.example"));
    }

    #[test]
    fn clear_removes_block_and_preserves_rest() {
        let existing = "127.0.0.1 localhost\n";
        let applied = splice_block(existing, Some(&render_block(&sample())));
        let cleared = splice_block(&applied, None);
        assert!(!cleared.contains(BEGIN));
        assert!(!cleared.contains(END));
        assert!(cleared.contains("127.0.0.1 localhost"));
        // Clearing an already-clean file is a no-op.
        assert_eq!(splice_block(&cleared, None), cleared);
    }

    #[test]
    fn block_entry_count_and_domains() {
        let applied = splice_block("", Some(&render_block(&sample())));
        assert_eq!(block_entry_count(&applied), Some(2));
        assert_eq!(block_domains(&applied), sample());
        assert_eq!(block_entry_count("no markers here"), None);
    }

    #[test]
    fn list_hash_is_deterministic_and_order_independent() {
        let a = list_hash(&["b.example".to_string(), "a.example".to_string()]);
        let b = list_hash(&["a.example".to_string(), "b.example".to_string()]);
        assert_eq!(a, b, "hash must be independent of input order");
        assert_eq!(a.len(), 16);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        // Dedup: a repeat does not change the hash.
        let c = list_hash(&[
            "a.example".to_string(),
            "a.example".to_string(),
            "b.example".to_string(),
        ]);
        assert_eq!(a, c);
    }

    #[test]
    fn validate_normalizes_sorts_and_dedups() {
        let out = validate_domains(&[
            "  B.Example.COM ".to_string(),
            "a.example.com".to_string(),
            "a.example.com".to_string(),
        ])
        .unwrap();
        assert_eq!(
            out,
            vec!["a.example.com".to_string(), "b.example.com".to_string()]
        );
    }

    #[test]
    fn validate_rejects_over_cap() {
        let big: Vec<String> = (0..MAX_DOMAINS + 1)
            .map(|i| format!("d{i}.example"))
            .collect();
        let err = validate_domains(&big).unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[test]
    fn validate_rejects_bad_charset() {
        let err = validate_domains(&["ok.example evil.example".to_string()]).unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[test]
    fn validate_blocks_reserved_names() {
        for reserved in [
            "localhost",
            "microsoft.com",
            "fe2.update.microsoft.com",
            "windowsupdate.com",
        ] {
            let err = validate_domains(&[reserved.to_string()]).unwrap_err();
            assert_eq!(err.0, ErrorCode::Blocked, "{reserved} must be blocked");
        }
    }

    /// Process-wide lock for tests touching the `KENNY_HOSTS_FILE` env var, mirroring the
    /// `KENNY_CONTROL_FILE` discipline in `control.rs`.
    fn with_hosts_file<T>(name: &str, f: impl FnOnce(&std::path::Path) -> T) -> T {
        let _guard = crate::control::TEST_ENV_LOCK.lock().unwrap();
        let path = std::env::temp_dir().join(name);
        let _ = std::fs::remove_file(&path);
        std::env::set_var(HOSTS_FILE_ENV, &path);
        let out = f(&path);
        std::env::remove_var(HOSTS_FILE_ENV);
        let _ = std::fs::remove_file(&path);
        out
    }

    #[test]
    fn apply_and_clear_hosts_core_round_trip() {
        with_hosts_file("kenny-webfilter-hosts.test", |path| {
            std::fs::write(path, "127.0.0.1 localhost\n").unwrap();
            let domains = validate_domains(&sample()).unwrap();
            let n = apply_hosts_only(path, &domains).unwrap();
            assert_eq!(n, 2);
            let content = std::fs::read_to_string(path).unwrap();
            assert!(content.contains("0.0.0.0 badsite.example"));
            assert!(content.contains("127.0.0.1 localhost"));

            // Idempotent re-apply.
            let before = std::fs::read_to_string(path).unwrap();
            apply_hosts_only(path, &domains).unwrap();
            assert_eq!(std::fs::read_to_string(path).unwrap(), before);

            let removed = clear_hosts_only(path).unwrap();
            assert_eq!(removed, 2);
            let after = std::fs::read_to_string(path).unwrap();
            assert!(!after.contains(BEGIN));
            assert!(after.contains("127.0.0.1 localhost"));
        });
    }

    #[test]
    fn apply_and_clear_succeed_when_hosts_file_is_read_only() {
        // Regression test: a hosts file pre-marked read-only (a common AV/anti-hijack
        // hardening default) must not make `webfilter_apply`/`clear` fail with
        // "access denied" — see `clear_readonly_if_present`.
        with_hosts_file("kenny-webfilter-hosts-readonly.test", |path| {
            std::fs::write(path, "127.0.0.1 localhost\n").unwrap();
            let mut perms = std::fs::metadata(path).unwrap().permissions();
            perms.set_readonly(true);
            std::fs::set_permissions(path, perms).unwrap();

            let domains = validate_domains(&sample()).unwrap();
            let n = apply_hosts_only(path, &domains).unwrap();
            assert_eq!(n, 2);
            let content = std::fs::read_to_string(path).unwrap();
            assert!(content.contains("0.0.0.0 badsite.example"));
            assert!(content.contains("127.0.0.1 localhost"));
            assert!(
                !std::fs::metadata(path).unwrap().permissions().readonly(),
                "the replaced file must not still be read-only"
            );

            // The replaced file is no longer read-only, but clearing again must be a
            // harmless no-op and clear must still succeed.
            let removed = clear_hosts_only(path).unwrap();
            assert_eq!(removed, 2);
            let after = std::fs::read_to_string(path).unwrap();
            assert!(!after.contains(BEGIN));
            assert!(after.contains("127.0.0.1 localhost"));
        });
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn status_ok_and_unsupported_off_windows() {
        // status is read-only: Ok even off Windows.
        let s = status(json!({})).await.unwrap();
        assert_eq!(s["supported"], false);
        assert_eq!(s["active"], false);
        assert_eq!(s["doh_policy"]["chrome"], "unknown");

        // apply/clear are unsupported off Windows (but still validate args first).
        let a = apply(json!({
            "domains": ["x.example"],
            "doh_policy": "disable",
            "list_hash": "deadbeefdeadbeef"
        }))
        .await
        .unwrap_err();
        assert_eq!(a.0, ErrorCode::Unsupported);

        let c = clear(json!({})).await.unwrap_err();
        assert_eq!(c.0, ErrorCode::Unsupported);
    }

    /// Randomized adversarial hosts-file content (including partial/duplicated/malformed
    /// BEGIN/END markers, multi-byte UTF-8 near the markers, and embedded NULs) fed through
    /// the splice/strip/count/hash core must never panic — a corrupted or hand-edited hosts
    /// file is untrusted input the same as any parsed file format.
    #[test]
    fn hosts_core_never_panics_on_random_adversarial_content() {
        struct Rng(u64);
        impl Rng {
            fn next(&mut self) -> u64 {
                self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
                let mut z = self.0;
                z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
                z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
                z ^ (z >> 31)
            }
        }
        let mut rng = Rng(0xBADC0FFEE0DDF00D);
        let fragments = [
            BEGIN,
            END,
            "\n",
            "0.0.0.0 ",
            "héllo.exämple.cöm\u{0}",
            "\u{1F600}",
            "\r\n",
            "# kenny-webfilter begin",
            "end (managed",
            "",
        ];
        for iter in 0..3000u32 {
            let mut s = String::new();
            let parts = 1 + (rng.next() % 12) as usize;
            for _ in 0..parts {
                s.push_str(fragments[(rng.next() as usize) % fragments.len()]);
            }
            let result = std::panic::catch_unwind(|| {
                let stripped = strip_block(&s);
                let _ = block_entry_count(&s);
                let domains = block_domains(&s);
                let _ = list_hash(&domains);
                let block = render_block(&domains);
                let _ = splice_block(&s, Some(&block));
                let _ = splice_block(&s, None);
                let _ = normalize_tail(&stripped);
            });
            if result.is_err() {
                panic!("iter {iter}: panic on input {s:?}");
            }
        }
    }

    /// Randomized domain strings (arbitrary bytes-as-UTF8, unicode, empty, huge) through
    /// `validate_domains` must only ever return `Err`, never panic.
    #[test]
    fn validate_domains_never_panics_on_random_input() {
        struct Rng(u64);
        impl Rng {
            fn next(&mut self) -> u64 {
                self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
                let mut z = self.0;
                z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
                z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
                z ^ (z >> 31)
            }
        }
        let mut rng = Rng(0x1234_5678_9ABC_DEF0);
        for iter in 0..3000u32 {
            let n = (rng.next() % 5) as usize;
            let mut domains = Vec::new();
            for _ in 0..n {
                let len = (rng.next() % 300) as usize;
                let s: String = (0..len)
                    .map(|_| char::from_u32((rng.next() % 0x11_0000) as u32).unwrap_or('?'))
                    .collect();
                domains.push(s);
            }
            let result = std::panic::catch_unwind(|| {
                let _ = validate_domains(&domains);
            });
            if result.is_err() {
                panic!("iter {iter}: panic on input {domains:?}");
            }
        }
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn apply_bad_args_and_blocked_surface_before_unsupported() {
        // A reserved name is refused with `blocked` even on a dev build.
        let blocked = apply(json!({
            "domains": ["localhost"],
            "doh_policy": "disable",
            "list_hash": "x"
        }))
        .await
        .unwrap_err();
        assert_eq!(blocked.0, ErrorCode::Blocked);

        // A bad doh_policy is `bad_args`.
        let bad = apply(json!({
            "domains": ["x.example"],
            "doh_policy": "nonsense",
            "list_hash": "x"
        }))
        .await
        .unwrap_err();
        assert_eq!(bad.0, ErrorCode::BadArgs);
    }
}
