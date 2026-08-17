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
