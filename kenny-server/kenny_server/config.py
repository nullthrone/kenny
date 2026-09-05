"""Runtime settings: a declarative catalog + a DB-over-env-over-default resolver.

kenny was historically configured entirely through ``KENNY_*`` environment
variables read via scattered ``os.environ.get`` calls. This module introduces a
single, structured layer so the operator can change the runtime-safe knobs from
the web dashboard without editing the environment and restarting.

Three pieces:

* :class:`SettingSpec` — one immutable descriptor per configurable value
  (type, default, label, validation, and a ``lifecycle`` flag), plus
* :data:`CATALOG` — the single source of truth listing every setting, driving
  both API validation and UI rendering, and
* :class:`Settings` — the resolver. Precedence is **DB override > env var >
  coded default**. Reads (:meth:`Settings.get`) are synchronous, lock-free dict
  lookups so per-request callers (e.g. the chat model) stay as cheap as the old
  ``os.environ.get``. The in-memory override map is authoritative and the
  SQLite ``settings`` table is only its durable mirror — the server is a single
  process on one event loop, so there is no cross-process invalidation problem.

``lifecycle`` is load-bearing:

* ``live``     — the consumer reads through :meth:`Settings.get` on every use (or
  an apply-hook re-applies on write), so an override takes effect immediately and
  survives a restart once :meth:`Settings.load` runs.
* ``restart``  — the consumer reads the value once, inside the app lifespan after
  :meth:`Settings.load`; an override applies on the next restart.
* ``env_only`` — never writable from the UI (auth/identity secrets, wire-contract
  knobs, and process-bind values read before settings load). Shown read-only for
  transparency.

``sensitive`` is an orthogonal axis, not a synonym for ``env_only``: it says the
value is never serialised back out (``describe()`` reports ``set``/``not set``),
whatever its lifecycle. The alert push channels are ``live`` *and* ``sensitive``
— writable from the dashboard, never readable back (ADR-0054).

Anything touching the agent wire contract stays ``env_only`` and is deferred to a
future ADR (see ADR for runtime settings).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .logging_config import apply_log_level

logger = logging.getLogger("kenny.config")

# Canonical boolean spellings (case-insensitive). Empty string counts as false.
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off", ""}

# Kept in sync with webfilter._DEFAULT_ADULT_URL / _DEFAULT_BYPASS_URL. These are
# the coded defaults; the live values now flow through this catalog.
_DEFAULT_ADULT_URL = (
    "https://raw.githubusercontent.com/StevenBlack/hosts/master/"
    "alternates/porn-only/hosts"
)
_DEFAULT_BYPASS_URL = (
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/"
    "domains/doh-vpn-proxy-bypass.txt"
)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class SettingSpec:
    """Immutable descriptor for one configurable setting.

    ``parse`` coerces a stored/env raw string into the typed value; ``validate``
    raises :class:`ValueError` for anything a write must reject (so an invalid
    value can never reach the DB and poison later reads).
    """

    key: str
    group: str
    type: str  # "bool" | "int" | "float" | "str" | "enum" | "secret"
    default_raw: str
    label: str
    help: str = ""
    lifecycle: str = "live"  # "live" | "restart" | "env_only"
    env: str | None = None  # env var name if different from key
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    sensitive: bool = False

    @property
    def env_name(self) -> str:
        return self.env or self.key

    @property
    def writable(self) -> bool:
        return self.lifecycle != "env_only"

    def parse(self, raw: str) -> Any:
        if self.type == "bool":
            return raw.strip().lower() in _BOOL_TRUE
        if self.type == "int":
            return int(raw)
        if self.type == "float":
            return float(raw)
        # str, enum and secret are all raw strings to the consumer.
        return raw

    def validate(self, raw: str) -> None:
        """Raise :class:`ValueError` if ``raw`` is not an acceptable value."""

        if self.type == "bool":
            if raw.strip().lower() not in (_BOOL_TRUE | _BOOL_FALSE):
                raise ValueError(f"{self.key}: expected a boolean, got {raw!r}")
            return
        if self.type in ("int", "float"):
            try:
                value = int(raw) if self.type == "int" else float(raw)
            except ValueError as exc:
                raise ValueError(f"{self.key}: expected {self.type}, got {raw!r}") from exc
            if self.min is not None and value < self.min:
                raise ValueError(f"{self.key}: must be >= {self.min:g}")
            if self.max is not None and value > self.max:
                raise ValueError(f"{self.key}: must be <= {self.max:g}")
            return
        if self.type == "enum":
            if self.choices is not None and raw not in self.choices:
                raise ValueError(
                    f"{self.key}: must be one of {', '.join(self.choices)}"
                )
            return
        # str / secret: any string is acceptable.


def _spec(key: str, group: str, type: str, default_raw: str, label: str, **kw: Any) -> SettingSpec:
    return SettingSpec(key=key, group=group, type=type, default_raw=default_raw, label=label, **kw)


# Group display order for the UI. Every spec's ``group`` must appear here.
GROUP_ORDER: tuple[str, ...] = (
    "Alerting & Digest",
    "Web filter",
    "Chat & AI",
    "Logging",
    "Network & Process",
    "Operator & Agent Auth",
    "Telemetry limits",
    "Agent distribution",
    "Backup",
    "Updates",
    "Discord & Tickets",
)


def group_slug(name: str) -> str:
    """URL-stable slug for a group name (the settings sidebar's ``#/settings/{slug}``).

    Derived from the display name rather than hand-assigned so every group is
    guaranteed one; a slug only changes if the group's *name* changes, at which
    point ``test_config.py`` pins the mapping so the break is caught, not silent.
    """

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_SPECS: list[SettingSpec] = [
    # -- Alerting & Digest (live; consumed by AlertEngine each pass) -----------
    _spec("KENNY_ALERT_COOLDOWN_SECS", "Alerting & Digest", "int", "3600",
          "Alert cooldown (s)", lifecycle="live", min=0,
          help="Per-scope suppression window bounding a flapping section to one "
               "alert plus one recovery per window."),
    _spec("KENNY_ALERT_OFFLINE_AFTER_SECS", "Alerting & Digest", "int", "2700",
          "Offline threshold (s)", lifecycle="live", min=0,
          help="An agent counts as offline when its newest snapshot is older "
               "than this and no live connection exists."),
    _spec("KENNY_ALERT_INTERVAL_SECS", "Alerting & Digest", "int", "60",
          "Evaluation interval (s)", lifecycle="live", min=0,
          help="Cadence of the alert loop. Changing it retimes the running "
               "loop. Setting it to 0 disables the loop only after a restart."),
    _spec("KENNY_ALERT_INITIAL_DELAY", "Alerting & Digest", "float", "10",
          "Initial evaluation delay (s)", lifecycle="restart", min=0,
          help="Delay before the first alert pass after startup."),
    _spec("KENNY_DIGEST_ENABLED", "Alerting & Digest", "bool", "1",
          "Weekly digest enabled", lifecycle="live"),
    _spec("KENNY_DIGEST_DAY", "Alerting & Digest", "enum", "mon",
          "Digest day", lifecycle="live", choices=_DAYS),
    _spec("KENNY_DIGEST_HOUR", "Alerting & Digest", "int", "8",
          "Digest hour (0-23)", lifecycle="live", min=0, max=23),
    # -- Alert push channels (live; resolved per dispatch by
    # notify.NotifierProvider through this resolver, so a change applies to the
    # next alert without a restart — ADR-0054). Every one of them is a secret:
    # an ntfy topic URL and a webhook URL are both bearer-equivalent, so they
    # are stored but never serialised back out (describe() reports set/not set).
    # Clearing one here turns the channel off; it does not fall back to the
    # environment. --------------------------------------------------------------
    _spec("KENNY_NTFY_URL", "Alerting & Digest", "secret", "",
          "ntfy topic URL", lifecycle="live", sensitive=True,
          help="ntfy.sh (or self-hosted) topic URL alerts are pushed to. "
               "Treated as sensitive: a topic URL is bearer-equivalent. "
               "Empty means the ntfy channel is off."),
    _spec("KENNY_NTFY_TOKEN", "Alerting & Digest", "secret", "",
          "ntfy access token", lifecycle="live", sensitive=True,
          help="Optional bearer token for an access-controlled ntfy topic."),
    _spec("KENNY_WEBHOOK_URL", "Alerting & Digest", "secret", "",
          "Generic alert webhook URL", lifecycle="live", sensitive=True,
          help="Incoming-webhook URL alerts are POSTed to, independent of the "
               "Discord alert webhook. Empty means the channel is off."),
    # -- Web filter (live; consumed by the refresh loop / ExternalListCache) ---
    _spec("KENNY_WEBFILTER_REFRESH_SECS", "Web filter", "int", "86400",
          "External list refresh (s)", lifecycle="live", min=0,
          help="Cadence for refreshing the external adult/bypass lists. "
               "Setting it to 0 disables the loop only after a restart."),
    _spec("KENNY_WEBFILTER_INITIAL_REFRESH_DELAY", "Web filter", "float", "5",
          "Initial refresh delay (s)", lifecycle="restart", min=0),
    _spec("KENNY_WEBFILTER_ADULT_URL", "Web filter", "str", _DEFAULT_ADULT_URL,
          "Adult blocklist URL", lifecycle="live",
          help="Source list of adult domains. Applied on the next refresh."),
    _spec("KENNY_WEBFILTER_BYPASS_URL", "Web filter", "str", _DEFAULT_BYPASS_URL,
          "Bypass/VPN blocklist URL", lifecycle="live",
          help="Source list of DoH/VPN/proxy bypass domains."),
    _spec("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS", "Web filter", "int", "5000",
          "Max block domains", lifecycle="live", min=1, max=10000,
          help="Cap on external adult domains pushed to an agent (hard cap 10000)."),
    # -- Chat & AI -------------------------------------------------------------
    _spec("KENNY_CHAT_MODEL", "Chat & AI", "str", "claude-sonnet-4-6",
          "Chat model", lifecycle="live",
          help="Anthropic model id used by Ask kenny."),
    _spec("ANTHROPIC_API_KEY", "Chat & AI", "secret", "",
          "Anthropic API key", lifecycle="env_only", sensitive=True,
          help="Gates the chat/recommendation features. Managed via environment."),
    # -- Logging ---------------------------------------------------------------
    _spec("KENNY_LOG_LEVEL", "Logging", "enum", "INFO",
          "Log level", lifecycle="live", choices=_LOG_LEVELS,
          help="Root/uvicorn/kenny log verbosity. Applied immediately."),
    _spec("KENNY_POLICY_CATALOG", "Logging", "str", "",
          "Policy catalog path", lifecycle="env_only",
          help="Path to the shared policy catalog file, loaded once at startup."),
    # -- Network & Process (read before settings load; read-only) --------------
    _spec("KENNY_HOST", "Network & Process", "str", "127.0.0.1",
          "Bind host", lifecycle="env_only"),
    _spec("KENNY_PORT", "Network & Process", "int", "8000",
          "Bind port", lifecycle="env_only"),
    _spec("KENNY_PUBLIC_URL", "Network & Process", "str", "",
          "Public base URL", lifecycle="env_only",
          help="External base URL used to build agent download links."),
    _spec("KENNY_DB_PATH", "Network & Process", "str", "kenny.sqlite",
          "Database path", lifecycle="env_only"),
    _spec("KENNY_TLS", "Network & Process", "bool", "0",
          "TLS-terminated deployment", lifecycle="env_only",
          help="Marks the deployment as behind TLS (secure cookie flag)."),
    # -- Operator & Agent Auth (secrets / wire-contract; read-only) ------------
    _spec("KENNY_OPERATOR_TOKEN", "Operator & Agent Auth", "secret", "",
          "Operator token", lifecycle="env_only", sensitive=True),
    _spec("KENNY_OPERATOR_TOKENS", "Operator & Agent Auth", "secret", "",
          "Additional operator tokens", lifecycle="env_only", sensitive=True),
    _spec("KENNY_AGENT_TOKENS", "Operator & Agent Auth", "secret", "",
          "Seed agent tokens", lifecycle="env_only", sensitive=True),
    _spec("KENNY_ALLOW_TOKEN_AUTH", "Operator & Agent Auth", "bool", "1",
          "Allow legacy token auth", lifecycle="env_only",
          help="Wire-contract knob for the agent handshake (deferred to a future ADR)."),
    _spec("KENNY_LOGIN_MAX_ATTEMPTS", "Operator & Agent Auth", "int", "5",
          "Login max attempts", lifecycle="env_only", min=1),
    _spec("KENNY_LOGIN_LOCKOUT_SECS", "Operator & Agent Auth", "float", "60",
          "Login lockout (s)", lifecycle="env_only", min=0),
    _spec("KENNY_SESSION_TTL_SECS", "Operator & Agent Auth", "int", "604800",
          "Login session lifetime (s)", lifecycle="env_only", min=60,
          help="How long a browser login session stays valid before re-login "
               "(default 7 days). Read at login time (ADR-0033)."),
    _spec("KENNY_OAUTH_ACCESS_TTL_SECS", "Operator & Agent Auth", "int", "3600",
          "OAuth access token lifetime (s)", lifecycle="env_only", min=1,
          help="MCP/Claude OAuth bearer token lifetime (default 1 hour). Read "
               "per-issuance by oauth.py, not through Settings."),
    _spec("KENNY_OAUTH_REFRESH_TTL_SECS", "Operator & Agent Auth", "int", "2592000",
          "OAuth refresh token lifetime (s)", lifecycle="env_only", min=1,
          help="MCP/Claude OAuth refresh token lifetime (default 30 days). Read "
               "per-issuance by oauth.py, not through Settings."),
    _spec("KENNY_FORWARDED_ALLOW_IPS", "Operator & Agent Auth", "str", "127.0.0.1",
          "Trusted proxy IPs (X-Forwarded-For)", lifecycle="env_only",
          help="Upstream addresses allowed to set X-Forwarded-For so the login "
               "rate-limiter sees the real client IP behind a reverse proxy. "
               "Default: loopback only. Read by uvicorn at startup."),
    _spec("KENNY_SERVER_PRIVATE_KEY", "Operator & Agent Auth", "secret", "",
          "Server private key (seed)", lifecycle="env_only", sensitive=True),
    _spec("KENNY_SERVER_PRIVATE_KEY_FILE", "Operator & Agent Auth", "str", "",
          "Server private key file", lifecycle="env_only"),
    _spec("KENNY_KEY_GRACE_SECS", "Operator & Agent Auth", "int", "604800",
          "Rotated key grace (s)", lifecycle="env_only",
          help="Wire-contract knob (deferred to a future ADR)."),
    _spec("KENNY_TOKEN_GRACE_SECS", "Operator & Agent Auth", "int", "604800",
          "Rotated token grace (s)", lifecycle="env_only",
          help="Wire-contract knob (deferred to a future ADR)."),
    # -- Telemetry limits (import-time framing guards; read-only) --------------
    _spec("KENNY_MAX_FRAME_BYTES", "Telemetry limits", "int", "8388608",
          "Max WS frame (bytes)", lifecycle="env_only"),
    _spec("KENNY_MAX_TELEMETRY_BYTES", "Telemetry limits", "int", "262144",
          "Max telemetry payload (bytes)", lifecycle="env_only"),
    _spec("KENNY_MAX_TELEMETRY_SECTIONS", "Telemetry limits", "int", "128",
          "Max telemetry sections", lifecycle="env_only"),
    _spec("KENNY_TELEMETRY_INTERVAL_SECS", "Telemetry limits", "int", "900",
          "Agent push interval (s)", lifecycle="env_only",
          help="Advertised to agents at install time. Agent-facing "
               "(deferred to a future ADR)."),
    _spec("KENNY_TELEMETRY_RETENTION_DAYS", "Telemetry limits", "int", "30",
          "Snapshot retention (days)", lifecycle="live", min=1,
          help="How long raw telemetry snapshots are kept. Snapshots dominate "
               "this database's size (~90 KB per row); lowering this is the "
               "main lever on disk usage. Lowering it prunes on the next alert "
               "cycle (~60s), not on a restart. Deleting rows frees space for "
               "reuse but does not shrink the database file — restore from a "
               "backup (ADR-0039) or VACUUM offline to reclaim disk."),
    _spec("KENNY_SQLITE_BUSY_TIMEOUT_MS", "Telemetry limits", "int", "20000",
          "SQLite busy timeout (ms)", lifecycle="env_only",
          help="How long a write waits for a contended SQLite lock before "
               "raising 'database is locked' (ADR-0051). Read once at import "
               "time, so it cannot be changed live from the dashboard."),
    # -- Agent distribution (read-only this iteration) -------------------------
    _spec("KENNY_GITHUB_REPO", "Agent distribution", "str", "nullthrone/kenny",
          "Agent GitHub repo", lifecycle="env_only"),
    _spec("KENNY_GITHUB_TOKEN", "Agent distribution", "secret", "",
          "GHCR token", lifecycle="env_only", sensitive=True,
          help="Only for polling a private kenny-server package on GHCR (ADR-0040). "
               "The agent binary and the changelog are read from GitHub anonymously "
               "(ADR-0057) and ignore this entirely."),
    _spec("KENNY_AGENT_VERSION", "Agent distribution", "str", "0.2.0",
          "Agent version", lifecycle="env_only"),
    _spec("KENNY_SERVER_VERSION", "Agent distribution", "str", "0.0.0-dev",
          "Server version", lifecycle="env_only"),
    _spec("KENNY_AGENT_BINARY", "Agent distribution", "str", "",
          "Agent binary path", lifecycle="env_only"),
    _spec("KENNY_AGENT_BINARY_CACHE", "Agent distribution", "str", "",
          "Agent binary cache dir", lifecycle="env_only"),
    # -- Backup (live; consumed by the backup loop / BackupManager) ------------
    _spec("KENNY_BACKUP_INTERVAL_SECS", "Backup", "int", "21600",
          "Backup interval (s)", lifecycle="live", min=0,
          help="Cadence of the automatic backup loop. Changing it retimes the "
               "running loop. Setting it to 0 disables the loop only after a "
               "restart."),
    _spec("KENNY_BACKUP_INITIAL_DELAY", "Backup", "float", "30",
          "Initial backup delay (s)", lifecycle="restart", min=0,
          help="Delay before the first automatic backup after startup."),
    _spec("KENNY_BACKUP_RETENTION", "Backup", "int", "7",
          "Backup retention (count)", lifecycle="live", min=1,
          help="Number of newest backups kept per target; older ones are pruned "
               "after each run."),
    _spec("KENNY_BACKUP_DIR", "Backup", "str", "",
          "Backup directory (env only)", lifecycle="env_only",
          help="Overrides the local backup directory. Empty derives it from "
               "KENNY_DB_PATH (a sibling 'backups' directory)."),
    # -- Updates (live; scheduled detection + operator-approved rollout, ADR-0040) --
    _spec("KENNY_UPDATE_CHECK_INTERVAL_SECS", "Updates", "int", "86400",
          "Update check interval (s)", lifecycle="live", min=0,
          help="Cadence of the scheduled check for newer agent releases (GitHub) "
               "and server images (GHCR). Changing it retimes the running loop. "
               "Setting it to 0 disables the loop only after a restart. Detection "
               "only stages/records what's available — it never rolls anything "
               "out on its own."),
    _spec("KENNY_UPDATE_CHECK_INITIAL_DELAY", "Updates", "float", "30",
          "Initial check delay (s)", lifecycle="restart", min=0,
          help="Delay before the first update check after startup."),
    _spec("KENNY_SERVER_IMAGE_REF", "Updates", "str", "ghcr.io/nullthrone/kenny-server",
          "Server image ref (GHCR)", lifecycle="live",
          help="GHCR repository polled (read-only, tags + manifest digest) to "
               "detect a newer server image. Never pulled or applied automatically "
               "— the operator runs the shown, digest-pinned compose command."),
    _spec("KENNY_AGENT_ROLLOUT_ON_CONNECT", "Updates", "bool", "0",
          "Auto-apply approved campaign on connect", lifecycle="live",
          help="When an operator has approved an agent update campaign, apply it "
               "automatically to agents as they connect/reconnect while the "
               "campaign is active. Off by default. Never enables a rollout by "
               "itself — an operator must still approve a campaign first."),
    _spec("KENNY_UPDATE_CAMPAIGN_MAX_AGE_SECS", "Updates", "int", "1209600",
          "Campaign max age (s)", lifecycle="live", min=0,
          help="An approved campaign auto-expires after this long even if not "
               "every agent reached the target version (default 14 days). It "
               "already auto-completes earlier once every known agent is on the "
               "target version."),
    # -- Discord & Tickets -----------------------------------------------------
    # Tickets are independent of Discord: the ticket store, lifecycle service and
    # sweeper run on every server. The Discord keys only decide whether the bot
    # surface is also connected.
    _spec("KENNY_DISCORD_BOT_TOKEN", "Discord & Tickets", "secret", "",
          "Discord bot token", lifecycle="env_only", sensitive=True,
          help="Bot token of the Discord application. Managed via the "
               "environment; without it the Discord surface stays off."),
    # The fourth alert push channel (ADR-0054): grouped with Discord because
    # that is where an operator looks for it, but resolved per dispatch by
    # notify.NotifierProvider exactly like the three in "Alerting & Digest".
    _spec("KENNY_DISCORD_WEBHOOK_URL", "Discord & Tickets", "secret", "",
          "Discord alert webhook URL", lifecycle="live", sensitive=True,
          help="Incoming-webhook URL used as a push notification channel for "
               "alerts. Independent of the bot surface. Empty means the "
               "channel is off."),
    _spec("KENNY_DISCORD_ENABLED", "Discord & Tickets", "bool", "0",
          "Discord bot enabled", lifecycle="restart",
          help="Connect the Discord bot surface at startup. Requires a bot "
               "token and at least one allowed guild."),
    _spec("KENNY_DISCORD_GUILD_IDS", "Discord & Tickets", "str", "",
          "Allowed guild IDs", lifecycle="restart",
          help="Comma-separated Discord server (guild) snowflakes kenny reacts "
               "in. EMPTY MEANS DENY EVERYWHERE — there is no allow-all mode."),
    _spec("KENNY_DISCORD_SUPPORT_CHANNEL_ID", "Discord & Tickets", "str", "",
          "Support channel ID", lifecycle="live",
          help="Channel snowflake where a mention opens a ticket. Empty accepts "
               "a mention in any channel of an allowed guild."),
    _spec("KENNY_DISCORD_OPERATOR_CHANNEL_ID", "Discord & Tickets", "str", "",
          "Operator channel ID", lifecycle="live",
          help="Channel snowflake where operator approval cards are posted. "
               "Empty posts them into the ticket thread instead."),
    _spec("KENNY_DISCORD_PRIVATE_THREADS", "Discord & Tickets", "bool", "1",
          "Use private threads", lifecycle="live",
          help="Open each ticket in a private thread with only the requester "
               "invited. Falls back to a public thread where the server plan "
               "does not allow private ones."),
    _spec("KENNY_DISCORD_MODEL", "Discord & Tickets", "str", "",
          "Discord model", lifecycle="live",
          help="Anthropic model id used on the Discord surface. Empty falls "
               "back to KENNY_CHAT_MODEL."),
    _spec("KENNY_DISCORD_MAX_TURNS_PER_TICKET", "Discord & Tickets", "int", "40",
          "Max assistant turns per ticket", lifecycle="live", min=1,
          help="Hard cap on autonomous turns; the ticket is handed to an "
               "operator once it is reached."),
    _spec("KENNY_DISCORD_RATE_LIMIT_PER_USER_HOUR", "Discord & Tickets", "int", "20",
          "Requests per user per hour", lifecycle="live", min=0,
          help="Per-account throttle on opening/driving tickets from Discord. "
               "0 means unlimited."),
    _spec("KENNY_TICKET_APPROVAL_TTL_SECS", "Discord & Tickets", "int", "86400",
          "Approval/consent lifetime (s)", lifecycle="live", min=0,
          help="How long a held tool call waits for a decision before the "
               "sweeper expires it (an expiry counts as a denial). 0 means the "
               "gate never expires."),
    _spec("KENNY_TRIAGE_ENABLED", "Discord & Tickets", "bool", "1",
          "Investigate new tickets automatically", lifecycle="live",
          help="On a new ticket, kenny runs one read-only investigation on the "
               "host and writes what it found into the ticket, before anyone is "
               "asked to look. Off means tickets arrive uninvestigated, as they "
               "did before."),
    _spec("KENNY_TRIAGE_RESOLVE", "Discord & Tickets", "bool", "0",
          "Let triage resolve a ticket", lifecycle="live",
          help="When an investigation both reaches a closing verdict AND can "
               "point at a read-only check that actually ran, resolve the "
               "ticket instead of only recommending it. Alert-opened tickets "
               "only; a resolved ticket stays reopenable for the auto-close "
               "window. Off means every verdict is a recommendation."),
    _spec("KENNY_TRIAGE_MAX_ITERATIONS", "Discord & Tickets", "int", "8",
          "Triage steps per ticket", lifecycle="live", min=1,
          help="How many model round-trips one investigation may take. Spending "
               "them all produces no verdict: the ticket stays open with what "
               "was found so far."),
    _spec("KENNY_TICKET_AUTOCLOSE_SECS", "Discord & Tickets", "int", "172800",
          "Auto-close resolved after (s)", lifecycle="live", min=0,
          help="Reopen window: a resolved ticket untouched for this long is "
               "closed by the sweeper. 0 disables auto-closing."),
    _spec("KENNY_TICKET_STALL_NUDGE_SECS", "Discord & Tickets", "int", "172800",
          "Stall reminder after (s)", lifecycle="live", min=0,
          help="A ticket blocked on a reply (from the requester or an operator) "
               "for this long gets one reminder from the sweeper. 0 disables "
               "reminders."),
    _spec("KENNY_TICKET_STALL_GIVEUP_SECS", "Discord & Tickets", "int", "604800",
          "Stall escalate-to-operator after (s)", lifecycle="live", min=0,
          help="A ticket still waiting on the requester after this long is "
               "re-blocked on an operator instead — the requester was not "
               "going to answer, so a human needs to pick it up. Never applies "
               "to a ticket already waiting on an operator. 0 disables "
               "escalation."),
    _spec("KENNY_TICKET_SWEEP_INTERVAL_SECS", "Discord & Tickets", "int", "300",
          "Ticket sweep interval (s)", lifecycle="live", min=0,
          help="Cadence of the housekeeping pass that expires overdue gates and "
               "auto-closes resolved tickets. Changing it retimes the running "
               "loop. Setting it to 0 disables the loop only after a restart."),
    _spec("KENNY_TICKET_SWEEP_INITIAL_DELAY", "Discord & Tickets", "float", "30",
          "Initial sweep delay (s)", lifecycle="restart", min=0,
          help="Delay before the first ticket sweep after startup."),
    _spec("KENNY_TICKET_RETENTION_DAYS", "Discord & Tickets", "int", "30",
          "Ticket transcript retention (days)", lifecycle="live", min=1,
          help="How long a closed ticket keeps its raw working transcript — the "
               "verbatim conversation and tool output needed only to resume it. "
               "The ticket, its summary and its audit trail are never pruned, so "
               "the record outlives the transcript by design."),
]

CATALOG: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}

# Apply-hooks run after a live setting's value changes (on write, reset, and once
# at startup for any DB override) so the change takes effect without a restart.
APPLY_HOOKS: dict[str, Callable[[Any], None]] = {
    "KENNY_LOG_LEVEL": apply_log_level,
}


class SettingNotWritable(Exception):
    """Raised when a write targets an ``env_only`` setting."""


class Settings:
    """DB-over-env-over-default resolver over :data:`CATALOG`."""

    def __init__(
        self,
        store: Any,
        catalog: Mapping[str, SettingSpec] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        apply_hooks: Mapping[str, Callable[[Any], None]] | None = None,
    ) -> None:
        self._store = store
        self._catalog = dict(catalog if catalog is not None else CATALOG)
        self._env = env if env is not None else os.environ
        self._apply_hooks = dict(apply_hooks if apply_hooks is not None else APPLY_HOOKS)
        self._overrides: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------------

    async def load(self) -> None:
        """Load DB overrides into memory and re-apply live apply-hooks once."""

        self._overrides = await self._store.all()
        for key in self._overrides:
            self._run_hook(key)

    # -- reads (hot path: synchronous, no I/O, no lock) ------------------------

    def get(self, key: str) -> Any:
        spec = self._catalog[key]
        raw, source = self._resolve_raw(key, spec)
        try:
            return spec.parse(raw)
        except (ValueError, TypeError):
            logger.warning(
                "invalid value for %s from %s (%r); falling back to default",
                key, source, raw,
            )
            return spec.parse(spec.default_raw)

    def effective(self, key: str) -> tuple[Any, str]:
        """Return ``(typed_value, source)`` where source is db/env/default."""

        spec = self._catalog[key]
        _raw, source = self._resolve_raw(key, spec)
        return self.get(key), source

    def _resolve_raw(self, key: str, spec: SettingSpec) -> tuple[str, str]:
        if key in self._overrides:
            return self._overrides[key], "db"
        env_val = self._env.get(spec.env_name)
        if env_val is not None and env_val != "":
            return env_val, "env"
        return spec.default_raw, "default"

    # -- writes (async; validate before persist) -------------------------------

    async def set(self, key: str, raw: str) -> None:
        spec = self._catalog[key]
        if not spec.writable:
            raise SettingNotWritable(f"{key} is managed via the environment")
        spec.validate(raw)
        await self._store.set(key, raw)
        self._overrides[key] = raw
        self._run_hook(key)

    async def reset(self, key: str) -> None:
        """Drop the DB override so ``key`` falls back to env/default."""

        spec = self._catalog[key]
        if not spec.writable:
            raise SettingNotWritable(f"{key} is managed via the environment")
        await self._store.delete(key)
        self._overrides.pop(key, None)
        self._run_hook(key)

    def _run_hook(self, key: str) -> None:
        hook = self._apply_hooks.get(key)
        if hook is None:
            return
        try:
            hook(self.get(key))
        except Exception:  # noqa: BLE001 - an apply-hook must never break a write
            logger.exception("apply-hook for %s failed", key)

    # -- serialisation for the API --------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Grouped catalog with effective values for ``GET /api/settings``.

        Secrets never expose their value: they report ``is_set`` instead.

        ``editable`` restates :attr:`SettingSpec.writable` on the wire so a
        console never has to re-derive it from ``lifecycle``. It is the same
        predicate ``PUT``/``DELETE`` enforce with a 403, read from the same
        property, so a control that renders can always be submitted and one
        that cannot be submitted never renders.
        """

        by_group: dict[str, list[dict[str, Any]]] = {g: [] for g in GROUP_ORDER}
        for key, spec in self._catalog.items():
            value, source = self.effective(key)
            row: dict[str, Any] = {
                "key": key,
                "group": spec.group,
                "type": spec.type,
                "label": spec.label,
                "help": spec.help,
                "lifecycle": spec.lifecycle,
                "editable": spec.writable,
                "source": source,
                "choices": list(spec.choices) if spec.choices else None,
                "min": spec.min,
                "max": spec.max,
                "sensitive": spec.sensitive,
            }
            if spec.sensitive:
                row["value"] = None
                row["is_set"] = source != "default"
                row["default"] = None
            else:
                row["value"] = value
                row["default"] = spec.parse(spec.default_raw)
            by_group.setdefault(spec.group, []).append(row)
        return [
            {"name": group, "slug": group_slug(group), "settings": by_group[group]}
            for group in GROUP_ORDER
            if by_group.get(group)
        ]

    def describe_one(self, key: str) -> dict[str, Any]:
        """Single-key effective view (used in write/reset responses)."""

        spec = self._catalog[key]
        value, source = self.effective(key)
        row: dict[str, Any] = {
            "key": key,
            "source": source,
            "lifecycle": spec.lifecycle,
            "editable": spec.writable,
        }
        if spec.sensitive:
            row["value"] = None
            row["is_set"] = source != "default"
        else:
            row["value"] = value
        return row
