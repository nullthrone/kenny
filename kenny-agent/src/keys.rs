//! Per-agent Ed25519 key material and the mutual-auth transcript (ADR-0022).
//!
//! Each agent owns an Ed25519 keypair whose **private seed never leaves the device**.
//! The raw 32-byte seed is persisted as `kenny-agent.key` in an update-stable directory
//! (the same shared `kenny` directory used by the kill-switch control file, NOT next to
//! the swappable binary, which ADR-0013 self-update replaces). The agent signs the
//! handshake transcript with this key and verifies the server's signature against a
//! **pinned** server public key delivered at install time.
//!
//! Keygen, signing, and verification are not Windows-gated: they build and test on Linux
//! CI. File-permission lockdown of the seed is best-effort and platform-specific: `0o600`
//! on unix, and on Windows an `icacls` DACL restricting the file to LocalSystem +
//! Administrators. The Windows lockdown matters because the seed lives in
//! `%ProgramData%\kenny`, the directory whose DACL the installer widens to
//! Authenticated Users : Modify (inheritable) so a standard user's tray can write the
//! kill-switch control file (ADR-0011); without an explicit restriction the key file
//! would inherit that grant and any local user could read or overwrite the agent's
//! identity. The lockdown is re-asserted on every start (see [`AgentKey::load_or_generate`]),
//! so the restart after a server-triggered self-update (ADR-0013) — which reuses the
//! persisted seed — also repairs a key written by an older agent that lacked it.

use std::path::PathBuf;

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey, VerifyingKey};

/// Domain-separation label prefixed to the transcript (20 ASCII bytes). See protocol.md.
const TRANSCRIPT_LABEL: &[u8] = b"kenny-mutual-auth-v1";

/// File name of the persisted raw 32-byte agent private seed.
pub const KEY_FILE: &str = "kenny-agent.key";

/// Environment override for the agent key-file path (tests / flexible deployments).
pub const KEY_FILE_ENV: &str = "KENNY_AGENT_KEY_FILE";

/// Resolve the agent key-file path.
///
/// Precedence: `KENNY_AGENT_KEY_FILE` override → the shared, update-stable `kenny`
/// directory (the same one that holds the kill-switch control file). The seed is
/// deliberately NOT stored next to the executable, which the self-updater swaps out.
pub fn key_path() -> PathBuf {
    if let Some(path) = std::env::var_os(KEY_FILE_ENV) {
        return PathBuf::from(path);
    }
    // Reuse the control file's parent directory as the update-stable base dir.
    match crate::control::control_path().parent() {
        Some(dir) => dir.join(KEY_FILE),
        None => PathBuf::from(KEY_FILE),
    }
}

/// The agent's Ed25519 signing key plus the path it was loaded from / persisted to.
pub struct AgentKey {
    signing: SigningKey,
}

impl AgentKey {
    /// Load the persisted agent key, or generate and persist a fresh one on first run.
    ///
    /// The raw 32-byte seed is read/written verbatim. On unix the file is locked to
    /// `0o600`; on Windows its DACL is restricted to LocalSystem + Administrators. A
    /// corrupt/short key file is an error rather than being silently overwritten, so a
    /// deployment problem is visible instead of rotating the agent's identity (which would
    /// break server-side pinning).
    ///
    /// The Windows lockdown runs on **both** paths — after first-run generation and after
    /// loading an existing seed — so it is idempotently re-asserted on every start,
    /// including the restart following a server-triggered self-update (ADR-0013). That
    /// repairs a seed persisted by an older agent that predated the lockdown, or one that
    /// inherited the widened `%ProgramData%\kenny` DACL (ADR-0011).
    pub fn load_or_generate() -> Result<Self> {
        let path = key_path();
        if path.exists() {
            let bytes = std::fs::read(&path)
                .with_context(|| format!("reading agent key {}", path.display()))?;
            let seed: [u8; 32] = bytes.as_slice().try_into().map_err(|_| {
                anyhow::anyhow!(
                    "agent key {} is {} bytes; expected a raw 32-byte Ed25519 seed",
                    path.display(),
                    bytes.len()
                )
            })?;
            // Re-assert the restrictive DACL on the persisted seed (Windows only; no-op
            // elsewhere). This is what makes the lockdown take effect right after a
            // server-triggered self-update, and repairs keys written by older agents.
            #[cfg(windows)]
            lock_down_key_file(&path);
            return Ok(Self {
                signing: SigningKey::from_bytes(&seed),
            });
        }

        // First run: generate a fresh keypair from the OS RNG and persist the seed.
        // getrandom 0.4 / rand_core 0.10 moved the OS RNG out of rand_core::OsRng into
        // getrandom::SysRng, which is fallible (TryRng); UnwrapErr adapts it to the
        // infallible CryptoRng that SigningKey::generate requires.
        use getrandom::SysRng;
        use rand_core::UnwrapErr;
        let signing = SigningKey::generate(&mut UnwrapErr(SysRng));
        let seed = signing.to_bytes();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating key dir {}", parent.display()))?;
        }
        write_locked_down(&path, &seed)
            .with_context(|| format!("writing agent key {}", path.display()))?;
        // Strip the inherited Authenticated-Users grant a freshly created file picks up
        // from `%ProgramData%\kenny` (Windows only; no-op elsewhere).
        #[cfg(windows)]
        lock_down_key_file(&path);
        Ok(Self { signing })
    }

    /// Construct from a raw 32-byte seed (used by tests).
    #[cfg(test)]
    pub fn from_seed(seed: [u8; 32]) -> Self {
        Self {
            signing: SigningKey::from_bytes(&seed),
        }
    }

    /// The agent's public key as standard base64 (with padding) — sent to the server
    /// at enrollment time.
    pub fn public_key_b64(&self) -> String {
        STANDARD.encode(self.signing.verifying_key().to_bytes())
    }

    /// Sign `transcript` and return the Ed25519 signature as standard base64.
    pub fn sign(&self, transcript: &[u8]) -> String {
        STANDARD.encode(self.signing.sign(transcript).to_bytes())
    }

    /// The agent's verifying (public) key, for in-process verification in tests.
    #[cfg(test)]
    pub fn verifying_key(&self) -> VerifyingKey {
        self.signing.verifying_key()
    }
}

/// Build the mutual-auth transcript, the single source of truth for the byte layout
/// shared by both signatures (and the golden-vector test). `client_nonce_raw` and
/// `server_nonce_raw` are the **raw** 32 bytes (base64-decoded), and `0x00` is a single
/// NUL separator:
///
/// ```text
/// label || 0x00 || agent_id || 0x00 || client_nonce || 0x00 || server_nonce
/// ```
pub fn build_transcript(
    agent_id: &str,
    client_nonce_raw: &[u8],
    server_nonce_raw: &[u8],
) -> Vec<u8> {
    let mut t = Vec::with_capacity(
        TRANSCRIPT_LABEL.len()
            + 1
            + agent_id.len()
            + 1
            + client_nonce_raw.len()
            + 1
            + server_nonce_raw.len(),
    );
    t.extend_from_slice(TRANSCRIPT_LABEL);
    t.push(0x00);
    t.extend_from_slice(agent_id.as_bytes());
    t.push(0x00);
    t.extend_from_slice(client_nonce_raw);
    t.push(0x00);
    t.extend_from_slice(server_nonce_raw);
    t
}

/// Decode and parse a pinned server public key from standard base64.
pub fn parse_server_pubkey(b64: &str) -> Result<VerifyingKey> {
    let raw = STANDARD
        .decode(b64.trim())
        .context("server public key is not valid base64")?;
    let bytes: [u8; 32] = raw.as_slice().try_into().map_err(|_| {
        anyhow::anyhow!(
            "server public key must decode to 32 bytes, got {}",
            raw.len()
        )
    })?;
    VerifyingKey::from_bytes(&bytes).context("server public key is not a valid Ed25519 point")
}

/// Verify a base64 Ed25519 `sig_b64` over `transcript` against a pinned server key.
pub fn verify_server_sig(
    server_key: &VerifyingKey,
    transcript: &[u8],
    sig_b64: &str,
) -> Result<()> {
    use ed25519_dalek::Verifier as _;
    let raw = STANDARD
        .decode(sig_b64.trim())
        .context("server signature is not valid base64")?;
    let bytes: [u8; 64] = raw.as_slice().try_into().map_err(|_| {
        anyhow::anyhow!(
            "server signature must decode to 64 bytes, got {}",
            raw.len()
        )
    })?;
    let sig = ed25519_dalek::Signature::from_bytes(&bytes);
    server_key
        .verify(transcript, &sig)
        .context("server signature failed verification against the pinned key")
}

/// Generate `n` fresh random bytes from the OS RNG and return them base64-encoded.
pub fn random_nonce_b64() -> String {
    use getrandom::SysRng;
    use rand_core::{Rng as _, UnwrapErr};
    let mut buf = [0u8; 32];
    UnwrapErr(SysRng).fill_bytes(&mut buf);
    STANDARD.encode(buf)
}

/// The `icacls` arguments that restrict the key file to LocalSystem + Administrators and
/// strip inherited ACEs (the Authenticated-Users grant it would pick up from
/// `%ProgramData%\kenny`, ADR-0011).
///
/// Pure and platform-neutral so the exact ACL is unit-testable on Linux CI even though the
/// invocation itself is Windows-only. Well-known SIDs are locale-proof, unlike the
/// localized group names.
#[cfg_attr(not(windows), allow(dead_code))]
fn key_lockdown_icacls_args(path: &std::path::Path) -> Vec<std::ffi::OsString> {
    use std::ffi::OsString;
    vec![
        path.as_os_str().to_os_string(),
        // Remove inherited ACEs — in particular the inheritable Authenticated Users grant
        // the installer places on the parent directory.
        OsString::from("/inheritance:r"),
        // Replace grants with exactly these two principals, Full control.
        OsString::from("/grant:r"),
        // S-1-5-18 = LocalSystem (the service account that runs the agent).
        OsString::from("*S-1-5-18:F"),
        // S-1-5-32-544 = the built-in Administrators group (for maintenance).
        OsString::from("*S-1-5-32-544:F"),
    ]
}

/// Restrict the persisted key file to LocalSystem + Administrators (Windows only).
///
/// Best-effort: a missing `icacls` or a non-zero exit is logged, never fatal — the agent
/// must still run, and a warning surfaces a lockdown that did not take. Idempotent, so it
/// is safe to call on every start.
#[cfg(windows)]
fn lock_down_key_file(path: &std::path::Path) {
    use std::os::windows::process::CommandExt;
    /// `CREATE_NO_WINDOW` — keep the helper headless during a foreground `run`.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    match std::process::Command::new("icacls")
        .args(key_lockdown_icacls_args(path))
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        Ok(o) if o.status.success() => {
            tracing::debug!(path = %path.display(), "restricted agent key file to SYSTEM + Administrators");
        }
        Ok(o) => tracing::warn!(
            path = %path.display(),
            status = %o.status,
            stderr = %String::from_utf8_lossy(&o.stderr).trim(),
            "icacls could not lock down the agent key; it may be readable by other local users"
        ),
        Err(e) => tracing::warn!(
            path = %path.display(),
            error = %e,
            "could not run icacls to lock down the agent key; it may be readable by other local users"
        ),
    }
}

/// Write `data` to `path`, restricting permissions to `0o600` on unix where supported.
fn write_locked_down(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt as _;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(path)?;
        f.write_all(data)?;
        f.sync_all()?;
        Ok(())
    }
    #[cfg(not(unix))]
    {
        std::fs::write(path, data)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transcript_matches_documented_layout() {
        // label(20) + 0x00 + "pc" + 0x00 + nonce(2) + 0x00 + nonce(2)
        let t = build_transcript("pc", &[1, 2], &[3, 4]);
        let mut want = Vec::new();
        want.extend_from_slice(b"kenny-mutual-auth-v1");
        want.push(0);
        want.extend_from_slice(b"pc");
        want.push(0);
        want.extend_from_slice(&[1, 2]);
        want.push(0);
        want.extend_from_slice(&[3, 4]);
        assert_eq!(t, want);
    }

    #[test]
    fn load_or_generate_persists_and_reloads_same_key() {
        // Serialize with the other tests that mutate KENNY_AGENT_KEY_FILE / global env.
        let _guard = crate::control::TEST_ENV_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!("kenny-key-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("kenny-agent.key");
        std::env::set_var(KEY_FILE_ENV, &path);

        let first = AgentKey::load_or_generate().unwrap();
        let pub1 = first.public_key_b64();
        // File exists and is exactly 32 bytes.
        assert_eq!(std::fs::read(&path).unwrap().len(), 32);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600, "key file must be 0o600");
        }

        // Reload: same public key (identity is stable across restarts).
        let second = AgentKey::load_or_generate().unwrap();
        assert_eq!(pub1, second.public_key_b64());

        std::env::remove_var(KEY_FILE_ENV);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sign_then_verify_round_trips() {
        let key = AgentKey::from_seed([7u8; 32]);
        let transcript = build_transcript("example-pc", &[9; 32], &[8; 32]);
        let sig_b64 = key.sign(&transcript);
        // Verify under the agent's own public key via the server-verify helper shape.
        let vk = key.verifying_key();
        verify_server_sig(&vk, &transcript, &sig_b64).expect("self-signed sig must verify");
    }

    #[test]
    fn key_lockdown_grants_only_system_and_admins() {
        // The exact DACL is security-critical: it must strip inheritance and grant Full
        // control to LocalSystem + Administrators only, never to any interactive-user SID.
        let path = std::path::Path::new(r"C:\ProgramData\kenny\kenny-agent.key");
        let strs: Vec<String> = key_lockdown_icacls_args(path)
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect();

        assert_eq!(strs[0], r"C:\ProgramData\kenny\kenny-agent.key");
        assert!(
            strs.iter().any(|s| s == "/inheritance:r"),
            "must strip inherited ACEs (the Authenticated-Users grant on the parent dir)"
        );
        assert!(
            strs.iter().any(|s| s == "*S-1-5-18:F"),
            "LocalSystem (the service) must keep full control"
        );
        assert!(
            strs.iter().any(|s| s == "*S-1-5-32-544:F"),
            "Administrators must keep full control"
        );
        // Must NOT grant any broad interactive principal: Authenticated Users (S-1-5-11),
        // Users (S-1-5-32-545), or Everyone (S-1-1-0).
        for forbidden in ["S-1-5-11", "S-1-5-32-545", "S-1-1-0"] {
            assert!(
                !strs.iter().any(|s| s.contains(forbidden)),
                "must not grant the key to {forbidden}"
            );
        }
    }

    #[test]
    fn parse_server_pubkey_rejects_garbage() {
        assert!(parse_server_pubkey("not base64!!!").is_err());
        assert!(parse_server_pubkey(&STANDARD.encode([0u8; 10])).is_err());
    }

    /// Randomized/malformed/truncated base64 and signature bytes through the handshake's
    /// parse+verify entry points (attacker-controlled: a `challenge` frame's `server_nonce`/
    /// `server_sig` come straight off the wire) must only ever return `Err`, never panic.
    #[test]
    fn handshake_parsing_never_panics_on_random_input() {
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
        let mut rng = Rng(0xFEED_FACE_CAFE_BABE);
        let dummy_key = AgentKey::from_seed([1u8; 32]).verifying_key();
        for iter in 0..3000u32 {
            let len = (rng.next() % 200) as usize;
            let raw: Vec<u8> = (0..len).map(|_| (rng.next() % 256) as u8).collect();
            // Exercise both raw-garbage strings and validly-base64-encoded random bytes.
            let s = if rng.next().is_multiple_of(2) {
                String::from_utf8_lossy(&raw).into_owned()
            } else {
                STANDARD.encode(&raw)
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let _ = parse_server_pubkey(&s);
                let _ = verify_server_sig(&dummy_key, b"transcript", &s);
            }));
            if result.is_err() {
                panic!("iter {iter}: panic on input {s:?}");
            }
        }
        // Also fuzz build_transcript with random (possibly non-UTF8-looking-but-valid)
        // agent_id strings and nonce byte lengths, including empty and huge.
        for iter in 0..500u32 {
            let id_len = (rng.next() % 100) as usize;
            let agent_id: String = (0..id_len)
                .map(|_| char::from_u32((rng.next() % 0x110000) as u32).unwrap_or('?'))
                .collect();
            let n1: Vec<u8> = (0..(rng.next() % 64)).map(|_| rng.next() as u8).collect();
            let n2: Vec<u8> = (0..(rng.next() % 64)).map(|_| rng.next() as u8).collect();
            let result = std::panic::catch_unwind(|| build_transcript(&agent_id, &n1, &n2));
            if result.is_err() {
                panic!("iter {iter}: panic on agent_id {agent_id:?}");
            }
        }
    }
}
