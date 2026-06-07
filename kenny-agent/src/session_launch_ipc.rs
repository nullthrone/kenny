//! Launch an allow-listed app in the interactive user session, via the tray helper.
//!
//! The agent runs as a LocalSystem service in **session 0** (no desktop), so it cannot
//! start a GUI app like Quick Assist where the user can see it. The tray helper
//! (`kenny-agent tray`) already runs in the interactive session, so — exactly like the
//! screenshot path (ADR-0018) — the service asks the tray to do it over a local named
//! pipe (`\\.\pipe\kenny-agent-session-launch`). Unlike the screencap pipe this one is
//! **duplex**: the service writes a launch request and reads back the result.
//!
//! **The allow-list is the trust boundary.** The tray launches *only* the executables in
//! [`ALLOWED_EXECUTABLES`]; the session-0 service can never have it start an arbitrary
//! program. See ADR-0022. Wire framing is shared with [`crate::ipc`].

use serde::{Deserialize, Serialize};

/// Executables the tray is permitted to launch on the service's behalf. This list IS the
/// trust boundary — keep it to known interactive remote-help binaries. Quick Assist
/// registers an app-execution alias (`quickassist.exe`) on the user's PATH. Room is left
/// to add `msra.exe` / `mstsc.exe` here later (LAN/VPN scenarios) without widening the
/// mechanism. See ADR-0022.
#[cfg_attr(not(windows), allow(dead_code))]
pub const ALLOWED_EXECUTABLES: &[&str] = &["quickassist.exe"];

/// Whether `exe` is on the launch allow-list (case-insensitive; bare file name only).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn is_allowed(exe: &str) -> bool {
    ALLOWED_EXECUTABLES
        .iter()
        .any(|a| a.eq_ignore_ascii_case(exe))
}

/// Request sent service → tray: which allow-listed executable to launch.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug, Clone, Serialize, Deserialize)]
struct LaunchRequest {
    exe: String,
}

/// Reply sent tray → service: success + child PID, or a failure reason.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug, Clone, Serialize, Deserialize)]
struct LaunchReply {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

/// Named-pipe path shared by the tray server and the service client.
#[cfg(windows)]
const PIPE_NAME: &str = r"\\.\pipe\kenny-agent-session-launch";

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::ffi::OsStr;
    use std::io::{self, Read, Write};
    use std::os::windows::ffi::OsStrExt;
    use std::time::{Duration, Instant};

    use serde_json::{json, Value};
    use tracing::{info, warn};
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED, HANDLE,
        INVALID_HANDLE_VALUE,
    };
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FlushFileBuffers, ReadFile, WriteFile, FILE_FLAGS_AND_ATTRIBUTES,
        FILE_SHARE_MODE, OPEN_EXISTING, PIPE_ACCESS_DUPLEX,
    };
    use windows::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, WaitNamedPipeW,
        PIPE_READMODE_BYTE, PIPE_TYPE_BYTE, PIPE_WAIT,
    };

    use crate::ipc::{read_frame, write_frame};
    use crate::protocol::ErrorCode;

    /// `GENERIC_READ | GENERIC_WRITE` — the client both writes the request and reads the
    /// reply, so it opens the duplex pipe for both directions.
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    /// Advisory pipe buffer hint (requests/replies are tiny JSON blobs).
    const PIPE_BUFFER: u32 = 4 * 1024;
    /// How long the service client waits for the tray to answer before giving up.
    const CLIENT_TIMEOUT: Duration = Duration::from_secs(5);

    /// Encode a NUL-terminated UTF-16 buffer for a Win32 `*W` call.
    fn wide(s: &str) -> Vec<u16> {
        OsStr::new(s)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    }

    /// A `std::io` adapter over a pipe `HANDLE` so the framing helpers can drive it.
    struct PipeStream(HANDLE);

    impl Read for PipeStream {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let mut read: u32 = 0;
            // SAFETY: `buf` is a valid mutable slice; `read` is a local out-param.
            unsafe { ReadFile(self.0, Some(buf), Some(&mut read), None) }
                .map_err(io::Error::other)?;
            Ok(read as usize)
        }
    }

    impl Write for PipeStream {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            let mut written: u32 = 0;
            // SAFETY: `buf` is a valid slice; `written` is a local out-param.
            unsafe { WriteFile(self.0, Some(buf), Some(&mut written), None) }
                .map_err(io::Error::other)?;
            Ok(written as usize)
        }
        fn flush(&mut self) -> io::Result<()> {
            // Push any buffered bytes out toward the peer. This does *not* on its own
            // close the discard-on-disconnect race (see `wait_for_client_disconnect`):
            // contrary to its docs, `FlushFileBuffers` does not reliably block until the
            // peer's read on a duplex pipe, which is why the launch reply was still being
            // dropped with ERROR_PIPE_NOT_CONNECTED (0x800700E9).
            // SAFETY: `self.0` is a valid pipe handle we own and opened for writing.
            unsafe { FlushFileBuffers(self.0) }.map_err(io::Error::other)
        }
    }

    /// Tray-side server loop: launch an allow-listed app for each connecting client.
    ///
    /// Runs on a dedicated thread for the life of the tray process. Each iteration creates
    /// a fresh single-instance duplex pipe, blocks until the service connects, reads one
    /// launch request, launches the app **in this (interactive) session**, and writes back
    /// the reply. Per-iteration errors are logged and the loop continues.
    pub fn serve() {
        let name = wide(PIPE_NAME);
        loop {
            // SAFETY: `name` is a valid NUL-terminated wide string; default security
            // attributes (None) give a DACL that lets LocalSystem connect.
            let pipe = unsafe {
                CreateNamedPipeW(
                    PCWSTR(name.as_ptr()),
                    PIPE_ACCESS_DUPLEX,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    1,
                    PIPE_BUFFER,
                    PIPE_BUFFER,
                    0,
                    None,
                )
            };
            if pipe == INVALID_HANDLE_VALUE {
                warn!("CreateNamedPipeW (session-launch) failed; retrying shortly");
                std::thread::sleep(Duration::from_secs(1));
                continue;
            }

            // Block until the service connects (ERROR_PIPE_CONNECTED == already there).
            // SAFETY: `pipe` is a valid pipe handle we own.
            let connected = unsafe { ConnectNamedPipe(pipe, None) };
            let ok = connected.is_ok() || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
            if ok {
                serve_one(pipe);
            }
            // SAFETY: `pipe` is valid; disconnect then close before the next round.
            unsafe {
                let _ = DisconnectNamedPipe(pipe);
                let _ = CloseHandle(pipe);
            }
        }
    }

    /// Read one launch request, enforce the allow-list, launch, and write the reply.
    fn serve_one(pipe: HANDLE) {
        let mut stream = PipeStream(pipe);
        let reply = match read_frame(&mut stream) {
            Ok(bytes) => handle_request(&bytes),
            Err(e) => {
                warn!(error = %e, "failed to read session-launch request");
                return;
            }
        };
        let body = serde_json::to_vec(&reply).unwrap_or_default();
        if let Err(e) = write_frame(&mut stream, &body) {
            warn!(error = %e, "failed to send session-launch reply");
            return;
        }
        // The reply now sits in the pipe buffer. `serve()` calls `DisconnectNamedPipe`
        // the instant we return, and that discards anything the client has not yet read —
        // so block until the client has drained the reply and closed its end first.
        wait_for_client_disconnect(pipe);
    }

    /// Block until the connected client closes its end of the duplex pipe.
    ///
    /// The service client closes its handle only *after* `read_frame` has handed it the
    /// full reply, so this blocking `ReadFile` unblocks (with end-of-pipe /
    /// `ERROR_BROKEN_PIPE`) exactly when the exchange is complete. Waiting for that before
    /// `serve()` disconnects is what actually guarantees the reply is never discarded
    /// mid-flight — `FlushFileBuffers` does not reliably do so (see `PipeStream::flush`).
    fn wait_for_client_disconnect(pipe: HANDLE) {
        let mut scratch = [0u8; 1];
        let mut read: u32 = 0;
        // SAFETY: `pipe` is a valid duplex pipe handle we own; `scratch`/`read` are local
        // out-params. The client never writes again after its request, so we expect either
        // 0 bytes or a broken-pipe error — the outcome is intentionally ignored.
        let _ = unsafe { ReadFile(pipe, Some(&mut scratch), Some(&mut read), None) };
    }

    /// Parse a request, refuse anything off the allow-list, and spawn it in this session.
    fn handle_request(bytes: &[u8]) -> LaunchReply {
        let req: LaunchRequest = match serde_json::from_slice(bytes) {
            Ok(r) => r,
            Err(e) => return LaunchReply::err(format!("bad launch request: {e}")),
        };
        if !is_allowed(&req.exe) {
            warn!(exe = %req.exe, "refusing to launch non-allow-listed executable");
            return LaunchReply::err(format!("'{}' is not on the launch allow-list", req.exe));
        }
        // The tray already runs in the interactive user session, so a plain spawn lands on
        // the visible desktop — no token impersonation needed (cf. ADR-0018).
        match std::process::Command::new(&req.exe).spawn() {
            Ok(child) => {
                info!(exe = %req.exe, pid = child.id(), "launched app in user session");
                LaunchReply::ok(child.id())
            }
            Err(e) => LaunchReply::err(format!("failed to launch '{}': {e}", req.exe)),
        }
    }

    /// Service-side client: ask the tray to launch `exe`, returning the
    /// `{launched, pid}` shape on success. A missing pipe means no interactive tray
    /// (nobody logged in) — surfaced as a clear error, mirroring `screen_capture`.
    pub fn launch_via_tray(exe: &str) -> Result<Value, (ErrorCode, String)> {
        // Defense in depth: also refuse off-list names on the service side.
        if !is_allowed(exe) {
            return Err((
                ErrorCode::Unsupported,
                format!("'{exe}' is not on the launch allow-list"),
            ));
        }
        let handle = open_with_retry()?;
        let mut stream = PipeStream(handle);
        let body = serde_json::to_vec(&LaunchRequest {
            exe: exe.to_string(),
        })
        .map_err(|e| (ErrorCode::Internal, e.to_string()))?;
        let result = (|| {
            write_frame(&mut stream, &body)
                .map_err(|e| (ErrorCode::Internal, format!("sending launch request: {e}")))?;
            read_frame(&mut stream)
                .map_err(|e| (ErrorCode::Internal, format!("reading launch reply: {e}")))
        })();
        // SAFETY: `handle` is a valid handle we opened above.
        unsafe {
            let _ = CloseHandle(handle);
        }
        let reply: LaunchReply = serde_json::from_slice(&result?)
            .map_err(|e| (ErrorCode::Internal, format!("bad launch reply: {e}")))?;
        if reply.ok {
            Ok(json!({ "launched": true, "pid": reply.pid }))
        } else {
            Err((
                ErrorCode::ExecFailed,
                reply.error.unwrap_or_else(|| "launch failed".to_string()),
            ))
        }
    }

    /// Whether the tray's launch pipe is reachable right now (a single, non-retrying
    /// probe). Used by `remotehelp_status` to report `interactive_session`.
    pub fn tray_available() -> bool {
        let name = wide(PIPE_NAME);
        // SAFETY: `name` is a valid NUL-terminated wide string.
        let opened = unsafe {
            CreateFileW(
                PCWSTR(name.as_ptr()),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_MODE(0),
                None,
                OPEN_EXISTING,
                FILE_FLAGS_AND_ATTRIBUTES(0),
                None,
            )
        };
        match opened {
            Ok(handle) => {
                // SAFETY: a valid handle we just opened.
                unsafe {
                    let _ = CloseHandle(handle);
                }
                true
            }
            // ERROR_PIPE_BUSY means the pipe exists (tray up) but is mid-request.
            Err(_) => (unsafe { GetLastError() }) == ERROR_PIPE_BUSY,
        }
    }

    /// Open the tray's duplex pipe, retrying briefly while it is busy or not yet up.
    fn open_with_retry() -> Result<HANDLE, (ErrorCode, String)> {
        let name = wide(PIPE_NAME);
        let deadline = Instant::now() + CLIENT_TIMEOUT;
        loop {
            // SAFETY: `name` is a valid NUL-terminated wide string.
            let opened = unsafe {
                CreateFileW(
                    PCWSTR(name.as_ptr()),
                    GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_MODE(0),
                    None,
                    OPEN_EXISTING,
                    FILE_FLAGS_AND_ATTRIBUTES(0),
                    None,
                )
            };
            match opened {
                Ok(handle) => return Ok(handle),
                Err(_) => {
                    if Instant::now() >= deadline {
                        return Err((
                            ErrorCode::Internal,
                            "no interactive session / tray not available to launch app".to_string(),
                        ));
                    }
                    // SAFETY: reading the thread-local last-error code.
                    let last = unsafe { GetLastError() };
                    if last == ERROR_PIPE_BUSY {
                        // SAFETY: `name` is valid; wait up to 1s for a free instance.
                        let _ = unsafe { WaitNamedPipeW(PCWSTR(name.as_ptr()), 1000) };
                    } else {
                        // Pipe not up yet (tray still starting) or transient — back off.
                        std::thread::sleep(Duration::from_millis(200));
                    }
                }
            }
        }
    }

    impl LaunchReply {
        fn ok(pid: u32) -> Self {
            Self {
                ok: true,
                pid: Some(pid),
                error: None,
            }
        }
        fn err(message: String) -> Self {
            Self {
                ok: false,
                pid: None,
                error: Some(message),
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::thread;

        /// A separate pipe name so the regression test never collides with a real tray.
        const TEST_PIPE: &str = r"\\.\pipe\kenny-agent-session-launch-test";

        /// Regression test for the ERROR_PIPE_NOT_CONNECTED (0x800700E9) launch failure.
        ///
        /// Reproduces the tray's serve()/serve_one() teardown around a canned reply (no
        /// process spawn) on a real Win32 named pipe, and has the client deliberately wait
        /// before reading. Without `wait_for_client_disconnect`, `DisconnectNamedPipe` would
        /// run first and discard the buffered reply, so the client's read would fail — the
        /// exact bug. With the fix the server blocks until the client has drained and closed,
        /// so the reply is always delivered. The pre-read delay makes the distinction
        /// deterministic rather than timing-dependent.
        #[test]
        fn reply_survives_server_teardown() {
            const PAYLOAD: &[u8] = b"{\"ok\":true,\"pid\":1234}";

            let server = thread::spawn(|| {
                let name = wide(TEST_PIPE);
                // SAFETY: `name` is a valid NUL-terminated wide string; mirrors `serve()`.
                let pipe = unsafe {
                    CreateNamedPipeW(
                        PCWSTR(name.as_ptr()),
                        PIPE_ACCESS_DUPLEX,
                        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                        1,
                        PIPE_BUFFER,
                        PIPE_BUFFER,
                        0,
                        None,
                    )
                };
                assert!(pipe != INVALID_HANDLE_VALUE, "CreateNamedPipeW failed");
                // SAFETY: `pipe` is a valid pipe handle we own.
                let connected = unsafe { ConnectNamedPipe(pipe, None) };
                let ok = connected.is_ok() || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
                assert!(ok, "ConnectNamedPipe failed");

                let mut stream = PipeStream(pipe);
                write_frame(&mut stream, PAYLOAD).expect("write reply");
                // The fix under test: block until the client has drained and closed.
                wait_for_client_disconnect(pipe);
                // SAFETY: `pipe` is valid; tear it down exactly like `serve()`.
                unsafe {
                    let _ = DisconnectNamedPipe(pipe);
                    let _ = CloseHandle(pipe);
                }
            });

            // Client: open the pipe (retrying until the server has created it).
            let name = wide(TEST_PIPE);
            let mut handle = None;
            for _ in 0..100 {
                // SAFETY: `name` is a valid NUL-terminated wide string.
                let opened = unsafe {
                    CreateFileW(
                        PCWSTR(name.as_ptr()),
                        GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_MODE(0),
                        None,
                        OPEN_EXISTING,
                        FILE_FLAGS_AND_ATTRIBUTES(0),
                        None,
                    )
                };
                if let Ok(h) = opened {
                    handle = Some(h);
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let handle = handle.expect("client could not open test pipe");

            // Wait before reading: the buggy teardown would discard the reply in this window.
            thread::sleep(Duration::from_millis(150));
            let mut stream = PipeStream(handle);
            let got = read_frame(&mut stream).expect("reply must survive server teardown");
            // SAFETY: `handle` is a valid handle we opened above.
            unsafe {
                let _ = CloseHandle(handle);
            }
            assert_eq!(got, PAYLOAD);
            server.join().expect("server thread panicked");
        }
    }
}

#[cfg(windows)]
pub use windows_impl::{launch_via_tray, serve, tray_available};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allow_list_is_case_insensitive() {
        assert!(is_allowed("quickassist.exe"));
        assert!(is_allowed("QuickAssist.exe"));
        assert!(is_allowed("QUICKASSIST.EXE"));
    }

    #[test]
    fn allow_list_rejects_arbitrary_binaries() {
        assert!(!is_allowed("cmd.exe"));
        assert!(!is_allowed("powershell.exe"));
        assert!(!is_allowed("calc"));
        assert!(!is_allowed(""));
        // No path-qualified forms: the allow-list is by bare file name only.
        assert!(!is_allowed(r"C:\Windows\System32\quickassist.exe"));
    }

    #[test]
    fn launch_request_round_trips() {
        let req = LaunchRequest {
            exe: "quickassist.exe".to_string(),
        };
        let bytes = serde_json::to_vec(&req).unwrap();
        let back: LaunchRequest = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(back.exe, "quickassist.exe");
    }

    #[test]
    fn launch_reply_omits_empty_fields() {
        let ok = LaunchReply {
            ok: true,
            pid: Some(42),
            error: None,
        };
        let v = serde_json::to_value(&ok).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["pid"], 42);
        assert!(v.get("error").is_none());

        let err = LaunchReply {
            ok: false,
            pid: None,
            error: Some("nope".to_string()),
        };
        let v = serde_json::to_value(&err).unwrap();
        assert_eq!(v["ok"], false);
        assert!(v.get("pid").is_none());
        assert_eq!(v["error"], "nope");
    }
}
