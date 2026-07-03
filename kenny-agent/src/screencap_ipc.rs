//! Local screen-capture IPC between the session-0 service and the user-session tray.
//!
//! A GDI `BitBlt` only sees the desktop of the calling session, and the agent runs
//! as a LocalSystem service in **session 0** (no visible desktop). The tray helper
//! (`kenny-agent tray`) runs in the interactive user session, so it is the only
//! process that can actually grab the screen. This module wires the two together
//! over a local **named pipe**: the tray hosts a server that captures on demand and
//! the service connects as a client to fetch the PNG. See ADR-0018.
//!
//! Wire framing is a single length-prefixed blob (`u32` little-endian length, then
//! the PNG bytes), shared with [`crate::session_launch_ipc`] via [`crate::ipc`].

/// Named-pipe path shared by the tray server and the service client.
#[cfg(windows)]
const PIPE_NAME: &str = r"\\.\pipe\kenny-agent-screencap";

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::ffi::OsStr;
    use std::io::{self, Read, Write};
    use std::os::windows::ffi::OsStrExt;
    use std::time::{Duration, Instant};

    use serde_json::Value;

    use crate::ipc::{read_frame, write_frame};
    use tracing::{debug, warn};
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED, HANDLE,
        INVALID_HANDLE_VALUE,
    };
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FlushFileBuffers, ReadFile, WriteFile, FILE_FLAGS_AND_ATTRIBUTES,
        FILE_SHARE_MODE, OPEN_EXISTING, PIPE_ACCESS_OUTBOUND,
    };
    use windows::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, WaitNamedPipeW,
        PIPE_READMODE_BYTE, PIPE_TYPE_BYTE, PIPE_WAIT,
    };

    use crate::protocol::ErrorCode;

    /// `GENERIC_READ` access right (the client only reads the captured PNG).
    const GENERIC_READ: u32 = 0x8000_0000;
    /// Advisory output-buffer hint for the pipe (writes may exceed it).
    const PIPE_OUT_BUFFER: u32 = 64 * 1024;
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
            // A pipe server that calls `DisconnectNamedPipe` while bytes are still unread
            // *discards* them, and the peer's next read fails with ERROR_PIPE_NOT_CONNECTED
            // (0x800700E9). `serve()` disconnects right after `serve_one` returns, so flush
            // until the service has drained the PNG before we tear the pipe down.
            // SAFETY: `self.0` is a valid pipe handle we own and opened for writing.
            unsafe { FlushFileBuffers(self.0) }.map_err(io::Error::other)
        }
    }

    /// Tray-side server loop: capture the screen for each connecting client.
    ///
    /// Runs on a dedicated thread for the life of the tray process. Each iteration
    /// creates a fresh single-instance pipe, blocks until the service connects,
    /// grabs the primary display, and writes the PNG as one frame. Per-iteration
    /// errors are logged and the loop continues.
    pub fn serve() {
        let name = wide(PIPE_NAME);
        // Tighten the well-known pipe to SYSTEM (the service) + the interactive user only,
        // instead of the loose default DACL (ADR-0018). Best-effort: if we cannot read our
        // own token to build the descriptor, fall back to the default so capture still
        // works — the service still verifies the server's SID on connect.
        let security = match security::pipe_security() {
            Ok(s) => Some(s),
            Err(e) => {
                warn!(error = %e, "capture pipe falling back to default security");
                None
            }
        };
        loop {
            // Keep `sa` alive across the CreateNamedPipeW call below (the pointer borrows it).
            let sa = security.as_ref().map(|s| s.attributes());
            let sa_ptr = sa.as_ref().map(|s| s as *const _);
            // SAFETY: `name` is a valid NUL-terminated wide string; `sa_ptr`, when set,
            // points at a live SECURITY_ATTRIBUTES whose DACL admits SYSTEM + the user.
            let pipe = unsafe {
                CreateNamedPipeW(
                    PCWSTR(name.as_ptr()),
                    PIPE_ACCESS_OUTBOUND,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    1,
                    PIPE_OUT_BUFFER,
                    0,
                    0,
                    sa_ptr,
                )
            };
            if pipe == INVALID_HANDLE_VALUE {
                warn!("CreateNamedPipeW failed; retrying shortly");
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

    /// Capture and write a single PNG frame to a connected client.
    fn serve_one(pipe: HANDLE) {
        match crate::handlers::screenshot::grab_primary_png() {
            Ok(png) => {
                let mut stream = PipeStream(pipe);
                if let Err(e) = write_frame(&mut stream, &png) {
                    warn!(error = %e, "failed to send screenshot frame");
                }
                debug!(bytes = png.len(), "served screenshot to client");
            }
            Err(e) => warn!(error = %e, "screen grab failed in tray"),
        }
    }

    /// Service-side client: ask the tray to capture, returning the `screen_capture`
    /// response shape. A missing pipe means no interactive tray (e.g. nobody logged
    /// in) — surfaced as a clear error rather than a black frame.
    pub fn capture_via_tray() -> Result<Value, (ErrorCode, String)> {
        let handle = open_pipe_or_relaunch_tray()?;
        // Defeat pipe squatting (ADR-0018): only trust a pipe served by the interactive
        // console user's tray, never one a lower-privileged process pre-created.
        if let Err(e) = security::verify_pipe_server_is_console_user(handle) {
            // SAFETY: `handle` is a valid handle we opened; close it before returning.
            unsafe {
                let _ = CloseHandle(handle);
            }
            return Err((
                ErrorCode::Internal,
                format!("refusing screenshot from untrusted capture pipe: {e}"),
            ));
        }
        let mut stream = PipeStream(handle);
        let result = read_frame(&mut stream).map_err(|e| {
            (
                ErrorCode::Internal,
                format!("reading screenshot from tray failed: {e}"),
            )
        });
        // SAFETY: `handle` is a valid handle we opened above.
        unsafe {
            let _ = CloseHandle(handle);
        }
        let png = result?;
        Ok(crate::handlers::screenshot::png_to_response(&png))
    }

    /// Open the tray's capture pipe, healing a dead or absent tray on demand.
    ///
    /// The tray is load-bearing for capture but is only (re)launched at service start and
    /// at logon. If the user closed it or it crashed in between, its pipe is gone and every
    /// capture would otherwise hard-fail until the next service restart or logon. When the
    /// first open times out we ask the service to relaunch the tray into the active console
    /// session — the privilege the service (LocalSystem) already holds, so no new privilege
    /// is taken — and retry exactly once.
    ///
    /// The two failure paths are kept distinct so the field can see the real cause:
    /// a relaunch that fails for want of a console session / user token means nobody is
    /// logged in; a relaunch that succeeds but leaves the pipe absent means the tray is
    /// crashing on start.
    fn open_pipe_or_relaunch_tray() -> Result<HANDLE, (ErrorCode, String)> {
        if let Ok(handle) = open_with_retry() {
            return Ok(handle);
        }
        crate::service::launch_tray_in_active_session().map_err(|e| {
            (
                ErrorCode::Internal,
                format!("no interactive session to capture screen (nobody logged in?): {e}"),
            )
        })?;
        open_with_retry().map_err(|_| {
            (
                ErrorCode::Internal,
                "tray relaunched but its screen-capture pipe is still unavailable".to_string(),
            )
        })
    }

    /// Open the tray's pipe, retrying briefly while it is busy or not yet up.
    fn open_with_retry() -> Result<HANDLE, (ErrorCode, String)> {
        let name = wide(PIPE_NAME);
        let deadline = Instant::now() + CLIENT_TIMEOUT;
        loop {
            // SAFETY: `name` is a valid NUL-terminated wide string.
            let opened = unsafe {
                CreateFileW(
                    PCWSTR(name.as_ptr()),
                    GENERIC_READ,
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
                            "no interactive session / tray not available to capture screen"
                                .to_string(),
                        ));
                    }
                    // SAFETY: reading the thread-local last-error code.
                    let last = unsafe { GetLastError() };
                    if last == ERROR_PIPE_BUSY {
                        // SAFETY: `name` is valid; wait up to 1s for a free instance.
                        let _ = unsafe { WaitNamedPipeW(PCWSTR(name.as_ptr()), 1000) };
                    } else {
                        // Pipe not up yet (e.g. tray still starting) or a transient
                        // error — back off briefly and retry until the deadline.
                        std::thread::sleep(Duration::from_millis(200));
                    }
                }
            }
        }
    }

    /// Pipe hardening (ADR-0018): restrict the well-known capture pipe to SYSTEM + the
    /// interactive user, and let the service verify it is talking to that user's tray
    /// rather than a lower-privileged process that pre-created the well-known name.
    mod security {
        use super::*;
        use core::ffi::c_void;

        use windows::core::PWSTR;
        use windows::Win32::Foundation::{LocalFree, FALSE, HLOCAL};
        use windows::Win32::Security::Authorization::{
            ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
            SDDL_REVISION_1,
        };
        use windows::Win32::Security::{
            EqualSid, GetTokenInformation, TokenUser, PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES,
            TOKEN_QUERY, TOKEN_USER,
        };
        use windows::Win32::System::Pipes::GetNamedPipeServerProcessId;
        use windows::Win32::System::RemoteDesktop::{
            WTSGetActiveConsoleSessionId, WTSQueryUserToken,
        };
        use windows::Win32::System::Threading::{
            GetCurrentProcess, OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        /// An owned security descriptor for the capture pipe, freed on drop.
        pub struct PipeSecurity {
            sd: PSECURITY_DESCRIPTOR,
        }

        impl Drop for PipeSecurity {
            fn drop(&mut self) {
                if !self.sd.0.is_null() {
                    // SAFETY: `sd` was allocated by ConvertStringSecurityDescriptor... via
                    // LocalAlloc; free it exactly once with LocalFree.
                    unsafe {
                        let _ = LocalFree(HLOCAL(self.sd.0));
                    }
                }
            }
        }

        impl PipeSecurity {
            /// A SECURITY_ATTRIBUTES pointing at this descriptor. The result borrows `self`,
            /// so keep `self` alive until the `CreateNamedPipeW` call returns.
            pub fn attributes(&self) -> SECURITY_ATTRIBUTES {
                SECURITY_ATTRIBUTES {
                    nLength: core::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                    lpSecurityDescriptor: self.sd.0,
                    bInheritHandle: FALSE,
                }
            }
        }

        /// Build a protected DACL granting GENERIC_ALL to LocalSystem (`SY`, the service
        /// client) and the current user only — nobody else can open the pipe.
        pub fn pipe_security() -> Result<PipeSecurity, String> {
            let user_sid = current_user_sid_string()?;
            let sddl = format!("D:P(A;;GA;;;SY)(A;;GA;;;{user_sid})");
            let wide_sddl: Vec<u16> = sddl.encode_utf16().chain(std::iter::once(0)).collect();
            let mut sd = PSECURITY_DESCRIPTOR(std::ptr::null_mut());
            // SAFETY: `wide_sddl` is NUL-terminated; `sd` is a local out-param freed on drop.
            unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    PCWSTR(wide_sddl.as_ptr()),
                    SDDL_REVISION_1,
                    &mut sd,
                    None,
                )
            }
            .map_err(|e| format!("building pipe security descriptor failed: {e}"))?;
            Ok(PipeSecurity { sd })
        }

        /// Verify the pipe server process is owned by the interactive console user.
        ///
        /// The tray runs as that user, so a match is the tray; a mismatch is a squatter
        /// running as some other principal, which we reject.
        pub fn verify_pipe_server_is_console_user(pipe: HANDLE) -> Result<(), String> {
            let mut server_pid = 0u32;
            // SAFETY: `pipe` is a valid connected handle; `server_pid` is a local out-param.
            unsafe { GetNamedPipeServerProcessId(pipe, &mut server_pid) }
                .map_err(|e| format!("GetNamedPipeServerProcessId failed: {e}"))?;

            let server_buf = process_user_buffer(server_pid)?;
            let console_buf = console_user_buffer()?;
            // SAFETY: each buffer holds a TOKEN_USER whose `User.Sid` points inside it and
            // stays valid for the EqualSid call below.
            let server_sid = unsafe { (*(server_buf.as_ptr() as *const TOKEN_USER)).User.Sid };
            let console_sid = unsafe { (*(console_buf.as_ptr() as *const TOKEN_USER)).User.Sid };
            // SAFETY: both SID pointers are valid for the lifetime of their buffers.
            if unsafe { EqualSid(server_sid, console_sid) }.is_ok() {
                Ok(())
            } else {
                Err("capture-pipe server is not the interactive console user".to_string())
            }
        }

        /// SID string of the current process's user (for the pipe DACL).
        fn current_user_sid_string() -> Result<String, String> {
            let mut token = HANDLE::default();
            // SAFETY: GetCurrentProcess is a pseudo-handle; `token` is closed below.
            unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) }
                .map_err(|e| format!("OpenProcessToken(self) failed: {e}"))?;
            let buf = token_user_buffer(token);
            // SAFETY: `token` is a valid handle we own.
            unsafe {
                let _ = CloseHandle(token);
            }
            let buf = buf?;
            // SAFETY: `buf` holds a TOKEN_USER; `User.Sid` points inside it.
            let sid = unsafe { (*(buf.as_ptr() as *const TOKEN_USER)).User.Sid };
            let mut str_ptr = PWSTR::null();
            // SAFETY: `sid` is valid; the call allocates a string we LocalFree below.
            unsafe { ConvertSidToStringSidW(sid, &mut str_ptr) }
                .map_err(|e| format!("ConvertSidToStringSidW failed: {e}"))?;
            // SAFETY: `str_ptr` is a NUL-terminated wide string from the call above.
            let s = unsafe { str_ptr.to_string() }.map_err(|e| format!("bad SID string: {e}"));
            // SAFETY: `str_ptr` was LocalAlloc'd by ConvertSidToStringSidW; free it once.
            unsafe {
                let _ = LocalFree(HLOCAL(str_ptr.0 as *mut c_void));
            }
            s
        }

        /// Read a token's `TOKEN_USER` into an owned byte buffer.
        fn token_user_buffer(token: HANDLE) -> Result<Vec<u8>, String> {
            let mut len = 0u32;
            // First call sizes the buffer (documented to fail with ERROR_INSUFFICIENT_BUFFER).
            // SAFETY: the null-buffer/zero-length form is the documented size query.
            let _ = unsafe { GetTokenInformation(token, TokenUser, None, 0, &mut len) };
            if len == 0 {
                return Err("GetTokenInformation(TokenUser) returned zero size".to_string());
            }
            let mut buf = vec![0u8; len as usize];
            // SAFETY: `buf` is `len` bytes; TokenUser writes a TOKEN_USER + trailing SID.
            unsafe {
                GetTokenInformation(
                    token,
                    TokenUser,
                    Some(buf.as_mut_ptr() as *mut c_void),
                    len,
                    &mut len,
                )
            }
            .map_err(|e| format!("GetTokenInformation(TokenUser) failed: {e}"))?;
            Ok(buf)
        }

        /// `TOKEN_USER` buffer for a process by pid (the pipe server).
        fn process_user_buffer(pid: u32) -> Result<Vec<u8>, String> {
            // SAFETY: returns a handle we close below; QUERY_LIMITED_INFORMATION is enough
            // to read the token's user and works across integrity levels.
            let proc = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid) }
                .map_err(|e| format!("OpenProcess({pid}) failed: {e}"))?;
            let mut token = HANDLE::default();
            // SAFETY: `proc` is a valid process handle; `token` is closed below.
            let opened = unsafe { OpenProcessToken(proc, TOKEN_QUERY, &mut token) };
            let result = match opened {
                Ok(()) => token_user_buffer(token),
                Err(e) => Err(format!("OpenProcessToken(server) failed: {e}")),
            };
            // SAFETY: `token`/`proc` are valid handles we own; skip the null token handle.
            unsafe {
                if !token.is_invalid() {
                    let _ = CloseHandle(token);
                }
                let _ = CloseHandle(proc);
            }
            result
        }

        /// `TOKEN_USER` buffer for the user attached to the physical console.
        fn console_user_buffer() -> Result<Vec<u8>, String> {
            // SAFETY: returns the console session id, or 0xFFFFFFFF when none is attached.
            let session = unsafe { WTSGetActiveConsoleSessionId() };
            if session == u32::MAX {
                return Err("no active console session".to_string());
            }
            let mut token = HANDLE::default();
            // SAFETY: `session` is valid; needs SYSTEM (the service has it); token closed below.
            unsafe { WTSQueryUserToken(session, &mut token) }
                .map_err(|e| format!("WTSQueryUserToken failed: {e}"))?;
            let result = token_user_buffer(token);
            // SAFETY: `token` is a valid handle we own.
            unsafe {
                let _ = CloseHandle(token);
            }
            result
        }
    }
}

#[cfg(windows)]
pub use windows_impl::{capture_via_tray, serve};
