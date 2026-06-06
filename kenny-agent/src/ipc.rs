//! Shared local-IPC framing for the session-0 service ⇄ user-session tray pipes.
//!
//! The agent runs as a LocalSystem service in **session 0** (no visible desktop), while
//! the tray helper (`kenny-agent tray`) runs in the interactive user session. They talk
//! over local **named pipes** — one for screen capture ([`crate::screencap_ipc`],
//! ADR-0018) and one for launching remote-help apps ([`crate::session_launch_ipc`],
//! ADR-0022). Both use the same wire framing defined here.
//!
//! Framing is a single length-prefixed blob: a `u32` little-endian length, then that many
//! payload bytes. The helpers are platform-neutral so they can be unit-tested on Linux;
//! the pipe servers/clients that drive them are Windows-only.

use std::io::{self, Read, Write};

/// Read a length-prefixed frame (`u32` LE length + payload) from `r`.
///
/// Off Windows the pipe server/client are compiled out, so the only callers are the unit
/// tests; `allow(dead_code)` keeps the portable `cargo build` clean.
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
