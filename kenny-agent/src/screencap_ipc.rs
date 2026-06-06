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
//! the PNG bytes). The framing helpers are platform-neutral so they can be unit
//! tested on Linux; the pipe server/client are Windows-only.

use std::io::{self, Read, Write};

/// Read a length-prefixed frame (`u32` LE length + payload) from `r`.
///
/// Off Windows the pipe server/client are compiled out, so the only callers are
/// the unit tests; `allow(dead_code)` keeps the portable `cargo build` clean.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn read_frame<R: Read>(r: &mut R) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    r.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf) as usize;
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf)?;
    Ok(buf)
}

/// Write `payload` as a length-prefixed frame (`u32` LE length + payload) to `w`.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn write_frame<W: Write>(w: &mut W, payload: &[u8]) -> io::Result<()> {
    let len: u32 = payload
        .len()
        .try_into()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "frame too large"))?;
    w.write_all(&len.to_le_bytes())?;
    w.write_all(payload)?;
    w.flush()
}

/// Named-pipe path shared by the tray server and the service client.
#[cfg(windows)]
const PIPE_NAME: &str = r"\\.\pipe\kenny-agent-screencap";

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::time::{Duration, Instant};

    use serde_json::Value;
    use tracing::{debug, warn};
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED, HANDLE,
        INVALID_HANDLE_VALUE,
    };
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, ReadFile, WriteFile, FILE_FLAGS_AND_ATTRIBUTES, FILE_SHARE_MODE,
        OPEN_EXISTING, PIPE_ACCESS_OUTBOUND,
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
            Ok(())
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
        loop {
            // SAFETY: `name` is a valid NUL-terminated wide string; default security
            // attributes (None) give a DACL that lets LocalSystem connect.
            let pipe = unsafe {
                CreateNamedPipeW(
                    PCWSTR(name.as_ptr()),
                    PIPE_ACCESS_OUTBOUND,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    1,
                    PIPE_OUT_BUFFER,
                    0,
                    0,
                    None,
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
        let handle = open_with_retry()?;
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
}

#[cfg(windows)]
pub use windows_impl::{capture_via_tray, serve};

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frame_round_trips() {
        let payload = b"\x89PNG\r\n\x1a\n some bytes".to_vec();
        let mut buf = Vec::new();
        write_frame(&mut buf, &payload).unwrap();
        // Length prefix is little-endian u32.
        assert_eq!(&buf[..4], &(payload.len() as u32).to_le_bytes());
        let mut cur = Cursor::new(buf);
        let back = read_frame(&mut cur).unwrap();
        assert_eq!(back, payload);
    }

    #[test]
    fn empty_frame_round_trips() {
        let mut buf = Vec::new();
        write_frame(&mut buf, &[]).unwrap();
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur).unwrap().is_empty());
    }

    #[test]
    fn truncated_frame_errors() {
        // Claims 10 bytes but provides none.
        let mut buf = 10u32.to_le_bytes().to_vec();
        buf.truncate(4);
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur).is_err());
    }
}
