//! Filesystem tools: `fs_list`, `fs_search`, `fs_read`, `fs_disk_usage`.
//!
//! All portable via `std` + `sysinfo`; no Windows-only code here.

use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

use crate::protocol::ErrorCode;

/// Cap on bytes returned by `fs_read` before truncation.
const READ_CAP: usize = 256 * 1024;
/// Cap on results returned by `fs_search`.
const SEARCH_CAP: usize = 1000;

#[derive(Debug, Deserialize)]
struct PathArg {
    path: String,
}

/// `fs_list` — directory entries with size and is_dir.
pub fn list(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: PathArg = serde_json::from_value(args)
        .map_err(|e| (ErrorCode::BadArgs, format!("invalid fs_list args: {e}")))?;
    let dir = Path::new(&args.path);
    let read = std::fs::read_dir(dir).map_err(map_io)?;

    let mut entries = Vec::new();
    for ent in read.flatten() {
        let meta = ent.metadata().ok();
        let is_dir = meta.as_ref().map(|m| m.is_dir()).unwrap_or(false);
        let bytes = meta.as_ref().map(|m| m.len()).unwrap_or(0);
        entries.push(json!({
            "name": ent.file_name().to_string_lossy(),
            "is_dir": is_dir,
            "bytes": bytes,
        }));
    }
    Ok(json!({ "entries": entries }))
}

#[derive(Debug, Deserialize)]
struct SearchArgs {
    root: String,
    pattern: String,
}

/// `fs_search` — recursive case-insensitive substring match on filenames.
pub fn search(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: SearchArgs = serde_json::from_value(args)
        .map_err(|e| (ErrorCode::BadArgs, format!("invalid fs_search args: {e}")))?;
    let needle = args.pattern.to_lowercase();
    let mut matches = Vec::new();
    let mut stack: Vec<PathBuf> = vec![PathBuf::from(&args.root)];

    while let Some(dir) = stack.pop() {
        if matches.len() >= SEARCH_CAP {
            break;
        }
        let Ok(read) = std::fs::read_dir(&dir) else {
            continue;
        };
        for ent in read.flatten() {
            let path = ent.path();
            let name = ent.file_name().to_string_lossy().to_lowercase();
            if name.contains(&needle) {
                matches.push(path.to_string_lossy().to_string());
                if matches.len() >= SEARCH_CAP {
                    break;
                }
            }
            if ent.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                stack.push(path);
            }
        }
    }
    Ok(json!({ "matches": matches }))
}

/// `fs_read` — read a file, truncating at [`READ_CAP`] bytes.
pub fn read(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: PathArg = serde_json::from_value(args)
        .map_err(|e| (ErrorCode::BadArgs, format!("invalid fs_read args: {e}")))?;
    let bytes = std::fs::read(&args.path).map_err(map_io)?;
    let truncated = bytes.len() > READ_CAP;
    let slice = if truncated {
        &bytes[..READ_CAP]
    } else {
        &bytes[..]
    };
    Ok(json!({
        "content": String::from_utf8_lossy(slice),
        "truncated": truncated,
    }))
}

/// `fs_disk_usage` — per-volume capacity, shared with the `disk` collector.
pub fn disk_usage(_args: Value) -> Result<Value, (ErrorCode, String)> {
    Ok(json!({ "volumes": crate::telemetry::collectors::disk::volumes() }))
}

fn map_io(e: std::io::Error) -> (ErrorCode, String) {
    match e.kind() {
        std::io::ErrorKind::NotFound => (ErrorCode::NotFound, e.to_string()),
        _ => (ErrorCode::ExecFailed, e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list_and_read_round_trip() {
        let dir = std::env::temp_dir().join(format!("kenny-fs-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("hello.txt");
        std::fs::write(&file, "hi there").unwrap();

        let listed = list(json!({"path": dir.to_string_lossy()})).unwrap();
        let names: Vec<String> = listed["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap().to_string())
            .collect();
        assert!(names.contains(&"hello.txt".to_string()));

        let content = read(json!({"path": file.to_string_lossy()})).unwrap();
        assert_eq!(content["content"], "hi there");
        assert_eq!(content["truncated"], false);

        let found = search(json!({"root": dir.to_string_lossy(), "pattern": "hello"})).unwrap();
        assert_eq!(found["matches"].as_array().unwrap().len(), 1);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_missing_is_not_found() {
        let err = read(json!({"path": "/no/such/file/kenny"})).unwrap_err();
        assert_eq!(err.0, ErrorCode::NotFound);
    }

    #[test]
    fn disk_usage_has_volumes() {
        let v = disk_usage(json!({})).unwrap();
        assert!(v["volumes"].is_array());
    }
}
