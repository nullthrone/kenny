//! Capability handlers — one module per tool family in the catalog.
//!
//! `dispatch.rs` routes a `request.tool` name to the matching function here. Each
//! handler returns `Result<Value, (ErrorCode, String)>`, which the dispatcher maps
//! onto `response.result` / `response.error`.

pub mod accounts;
pub mod agent_update;
pub mod diagnostics;
pub mod fs;
pub mod network;
pub mod powershell;
pub mod remotehelp;
pub mod screenshot;
pub mod shell;
pub mod webfilter;
pub mod winget;
