//! Stamp the agent version, led by the GitHub release tag (ADR-0015).
//!
//! At release build time CI sets `KENNY_AGENT_VERSION=<tag>` (e.g. `v0.3.0`);
//! we expose it to the crate as the compile-time env `KENNY_BUILD_VERSION`.
//! For dev/CI builds without the tag set, we fall back to the Cargo package
//! version, so the binary always reports a sensible version.
//!
//! Also stamps the release channel (ADR-0048): CI sets `KENNY_AGENT_CHANNEL=dev` on
//! the `release-dev.yml` build; every other build (stable release, local `cargo
//! build`, CI's own test builds) leaves it unset and gets `stable`.

use std::env;
use std::path::{Path, PathBuf};

fn main() {
    let version = env::var("KENNY_AGENT_VERSION")
        .ok()
        .map(|v| v.trim().trim_start_matches('v').to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| env::var("CARGO_PKG_VERSION").unwrap());
    println!("cargo:rustc-env=KENNY_BUILD_VERSION={version}");
    println!("cargo:rerun-if-env-changed=KENNY_AGENT_VERSION");

    let channel = env::var("KENNY_AGENT_CHANNEL")
        .ok()
        .map(|c| c.trim().to_string())
        .filter(|c| !c.is_empty())
        .unwrap_or_else(|| "stable".to_string());
    println!("cargo:rustc-env=KENNY_BUILD_CHANNEL={channel}");
    println!("cargo:rerun-if-env-changed=KENNY_AGENT_CHANNEL");

    embed_deny_rules();

    // Embed a Windows application manifest. asInvoker (NOT requireAdministrator) is
    // deliberate: the same binary launches the tray in the standard-user session, which a
    // require-admin manifest would block. `setup` elevates at runtime instead. See ADR-0030.
    if std::env::var_os("CARGO_CFG_WINDOWS").is_some() {
        use embed_manifest::{embed_manifest, new_manifest};
        embed_manifest(new_manifest("Kenny.Agent")).expect("unable to embed manifest");

        // Embed a Windows VERSIONINFO resource + the exe icon so AV/anti-cheat heuristics
        // and the user see identifiable publisher software instead of an anonymous binary
        // (ADR-0035). We do NOT set a manifest on `winresource` — `embed_manifest` above
        // owns the (asInvoker) manifest, and `winresource` only emits a manifest when
        // `set_manifest`/`set_manifest_file` is called, so there is no duplicate resource.
        let mut res = winresource::WindowsResource::new();
        res.set("CompanyName", "kenny contributors");
        res.set("ProductName", "Kenny Agent");
        res.set(
            "FileDescription",
            "kenny outbound-tunnel remote-admin and telemetry agent",
        );
        res.set("OriginalFilename", "kenny-agent.exe");
        res.set("LegalCopyright", "kenny contributors — AGPL-3.0-only");
        // Reuse the same version string that leads `KENNY_BUILD_VERSION` (ADR-0015), so the
        // PE version matches what the agent reports on the wire.
        res.set("FileVersion", &version);
        res.set("ProductVersion", &version);
        res.set_icon("assets/kenny-on.ico");
        res.compile()
            .expect("unable to embed Windows version resource");
    }
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=assets/kenny-on.ico");
}

/// Copy the shared deny-rule catalog (`docs/policy/deny_rules.json`, the single source of
/// truth shared with the Python server, ADR-0020) into `OUT_DIR` so `src/policy.rs` can
/// embed it with `include_str!` without reaching outside the crate.
///
/// A raw `include_str!("../../docs/policy/deny_rules.json")` works for native builds (the
/// whole repo is checked out) but breaks the Linux release build: `cross` compiles inside a
/// container that mounts only the crate directory, so the repo-root `docs/` is not visible.
/// The release workflow therefore hands us the catalog's directory in `KENNY_DENY_RULES_DIR`
/// (mounted into the container via `Cross.toml`); everywhere else we fall back to the path
/// relative to the crate manifest.
fn embed_deny_rules() {
    let src = match env::var_os("KENNY_DENY_RULES_DIR") {
        Some(dir) => PathBuf::from(dir).join("deny_rules.json"),
        None => Path::new(&env::var("CARGO_MANIFEST_DIR").unwrap())
            .join("../docs/policy/deny_rules.json"),
    };
    let dst = Path::new(&env::var("OUT_DIR").unwrap()).join("deny_rules.json");
    std::fs::copy(&src, &dst)
        .unwrap_or_else(|e| panic!("failed to copy deny-rule catalog from {src:?}: {e}"));
    println!("cargo:rerun-if-changed={}", src.display());
    println!("cargo:rerun-if-env-changed=KENNY_DENY_RULES_DIR");
}
