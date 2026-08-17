import type { Role } from '../../api/types'

/**
 * `GET /api/me` — richer than the frozen `Me` (which only carries what the
 * Shell needs). Field names come from `userstore.py::_public_user` plus the
 * `hosts`/`is_shared_token` the route adds; `id`/`email`/`avatar`/
 * `totp_enabled`/`created_at` are real but not part of the shared contract,
 * so they live here rather than widening the frozen type.
 */
export interface ProfileMe {
  id: number | null
  username: string
  email: string | null
  role: Role
  avatar: string | null
  disabled?: boolean
  totp_enabled: boolean
  capability_profile?: string | null
  hosts: string[]
  created_at: string | null
  /** Legacy shared-token identity — no backing user row, no editable account. */
  is_shared_token: boolean
}

/** `GET /api/avatars` → `{avatars: [...]}`. Served as `/assets/{id}.png`. */
export interface AvatarsResponse {
  avatars: string[]
}

/** One row of `GET /api/me/pats` → `{pats: [...]}` (`userstore.py::list_pats`). */
export interface Pat {
  id: number
  label: string | null
  created_at: string
  last_used: string | null
  revoked: boolean
}

/** `POST /api/me/pats` — the plaintext token, shown exactly once. */
export interface PatCreateResponse {
  token: string
}

/** `POST /api/me/totp` (step 1) — scan-or-paste material for the authenticator app. */
export interface TotpSetup {
  secret: string
  uri: string
}

/**
 * One row of `GET /api/me/sessions` → `{sessions: [...]}`
 * (`webui/users.py::api_me_sessions`). The raw session id never leaves the
 * server — `current` is how this browser's own row is told apart from the
 * others.
 */
export interface SessionRow {
  created_at: string
  expires_at: string
  ip: string | null
  user_agent: string | null
  current: boolean
}

/** `POST /api/me/sessions/revoke-others` → `{ok, revoked}` — count of sessions ended. */
export interface RevokeOthersResponse {
  ok: boolean
  revoked: number
}

/**
 * One row of `GET /api/me/discord` → `bindings` (`webui/tickets.py::api_me_discord_get`).
 * Only the raw Discord account id (the snowflake) — no display name is ever
 * stored, since the only one the server sees lives on the mutable, unverified
 * `/link` claim, never on the identity itself (ADR-0044).
 */
export interface DiscordBinding {
  discord_user_id: string
  guild_id: string
  linked_at: string
  linked_via: string
}

/** `GET /api/me/discord` — never a 404; an unlinked account reads as `{linked: false, bindings: []}`. */
export interface DiscordMeStatus {
  linked: boolean
  bindings: DiscordBinding[]
  note: string
}

/** `DELETE /api/me/discord` → `{ok, removed}` — count of bindings removed. */
export interface DiscordUnlinkResponse {
  ok: boolean
  removed: number
}
