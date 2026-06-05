//! Stamp the agent version, led by the GitHub release tag (ADR-0014).
//!
//! At release build time CI sets `KENNY_AGENT_VERSION=<tag>` (e.g. `v0.3.0`);
//! we expose it to the crate as the compile-time env `KENNY_BUILD_VERSION`.
//! For dev/CI builds without the tag set, we fall back to the Cargo package
//! version, so the binary always reports a sensible version.

use std::env;

fn main() {
    let version = env::var("KENNY_AGENT_VERSION")
        .ok()
        .map(|v| v.trim().trim_start_matches('v').to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| env::var("CARGO_PKG_VERSION").unwrap());
    println!("cargo:rustc-env=KENNY_BUILD_VERSION={version}");
    println!("cargo:rerun-if-env-changed=KENNY_AGENT_VERSION");
}
