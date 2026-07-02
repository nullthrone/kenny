"""Parental-controls web filtering: matching, external lists, and the service facade.

This module owns the server-side half of the ``web_activity`` / ``webfilter_*``
feature (ADR-0026):

* :func:`normalize_domain` / :func:`matches` / :func:`classify` — the pure
  domain-matching core (suffix match, allow-precedence, layered categories).
* :func:`load_seed` — the shipped seed of well-known adult domains.
* :class:`ExternalListCache` — fetch + write-through disk cache of the external
  adult (StevenBlack) and bypass (hagezi) lists, with size guards and a
  seed/disk fallback when offline.
* :func:`effective_list` / :func:`build_apply_args` — the per-host layered list
  used for matching (flagging) vs the flat block set pushed to the agent.
* :class:`WebFilterService` — the async facade the tunnel, API, and MCP tools
  use; wraps a :class:`~kenny_server.store.WebFilterStore` + an
  :class:`ExternalListCache`.

The server is the authoritative matcher; the agent is a dumb, idempotent
enforcer. See ADR-0026 and ``docs/protocol.md`` for the contract shapes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .store import WebFilterStore

logger = logging.getLogger("kenny.webfilter")

# --- domain normalization / matching ------------------------------------------

# A single label: ASCII alnum/hyphen or any non-ASCII code point (IDNA
# passthrough — we keep unicode as-is rather than encode/reject it). Labels are
# validated after splitting on ".", so they never contain a dot themselves.
_LABEL_RE = re.compile(
    r"^[a-z0-9¡-￿](?:[a-z0-9¡-￿-]*[a-z0-9¡-￿])?$"
)


def normalize_domain(value: Any) -> str | None:
    """Normalize a host string to a bare lowercase domain, or ``None`` if invalid.

    Lowercases, strips a leading scheme, any path/query/userinfo/port, and a
    trailing dot. Requires at least two labels, rejects empty/oversized labels,
    IPv4 literals, and anything with illegal characters. Non-ASCII labels pass
    through unchanged (IDNA passthrough).
    """

    if not isinstance(value, str):
        return None
    d = value.strip().lower()
    if not d:
        return None
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0]  # strip path
    d = d.split("?", 1)[0]  # strip query
    d = d.rsplit("@", 1)[-1]  # strip userinfo
    d = d.split(":", 1)[0]  # strip port
    d = d.strip().rstrip(".")
    if not d:
        return None
    labels = d.split(".")
    if len(labels) < 2:
        return None
    if len(d) > 253:
        return None
    if all(label.isdigit() for label in labels):
        return None  # IPv4 literal, not a domain
    for label in labels:
        if not label or len(label) > 63 or not _LABEL_RE.match(label):
            return None
    return d


def matches(observed: str, entry: str) -> bool:
    """True when ``observed`` is ``entry`` or a subdomain of it (suffix match)."""

    return observed == entry or observed.endswith("." + entry)


# Category precedence when a domain is contributed by several layers: a custom
# entry always wins over the shipped/external lists.
_CATEGORY_PRIORITY = {"custom": 3, "seed": 2, "external_adult": 1, "bypass": 0}
Category = str  # "custom" | "seed" | "external_adult" | "bypass"


def classify(
    observed: str, effective: dict[str, Any]
) -> "tuple[Category, str] | None":
    """Classify one observed domain against the effective list.

    Returns ``(category, matched_entry)`` for the most specific matching block
    entry, or ``None`` when nothing matches or an equal-or-more-specific ``allow``
    entry overrides the block. ``effective`` is the structure from
    :func:`effective_list`.
    """

    blocks: dict[str, str] = effective["blocks"]
    allows: set[str] = effective["allows"]

    best_entry: str | None = None
    best_category: str | None = None
    for entry, category in blocks.items():
        if matches(observed, entry) and (best_entry is None or len(entry) > len(best_entry)):
            best_entry, best_category = entry, category
    if best_entry is None:
        return None

    best_allow: str | None = None
    for entry in allows:
        if matches(observed, entry) and (best_allow is None or len(entry) > len(best_allow)):
            best_allow = entry
    # An allow overrides a block only when it is equal-or-more-specific (longer
    # or equal suffix). A broader allow does not unblock a narrower block.
    if best_allow is not None and len(best_allow) >= len(best_entry):
        return None
    return best_category or "custom", best_entry


# --- shipped seed -------------------------------------------------------------

_SEED_PATH = Path(__file__).parent / "data" / "webfilter_seed.json"
_SEED_CACHE: frozenset[str] | None = None


def load_seed() -> frozenset[str]:
    """Return the shipped seed of adult domains (parsed + cached)."""

    global _SEED_CACHE
    if _SEED_CACHE is None:
        try:
            data = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
            domains = {
                nd for d in data.get("domains", []) if (nd := normalize_domain(d))
            }
        except (OSError, ValueError) as exc:  # pragma: no cover - packaging error
            logger.warning("failed to load webfilter seed: %s", exc)
            domains = set()
        _SEED_CACHE = frozenset(domains)
    return _SEED_CACHE


# --- external lists -----------------------------------------------------------

_DEFAULT_ADULT_URL = (
    "https://raw.githubusercontent.com/StevenBlack/hosts/master/"
    "alternates/porn-only/hosts"
)
_DEFAULT_BYPASS_URL = (
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/"
    "domains/doh-vpn-proxy-bypass.txt"
)

# Guards against a compromised/oversized upstream (CWE-400/770).
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_EXTERNAL_DOMAINS = 300_000
# Sink IPs a hosts file uses; the domain sits in the second column after these.
_SINK_IPS = {"0.0.0.0", "127.0.0.1", "::1", "255.255.255.255"}

_SOURCES = ("adult", "bypass")


def _parse_list(text: str) -> frozenset[str]:
    """Parse hosts-format or bare-domain-list text into a set of domains.

    Handles both formats: a ``0.0.0.0 domain`` hosts line yields the second
    column; a bare ``domain`` line yields the first. Comments (``#``) and sink
    IPs are skipped; each candidate is normalized; the result is capped.
    """

    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] in _SINK_IPS:
            candidate = parts[1]
        else:
            candidate = parts[0]
        if candidate in _SINK_IPS:
            continue
        nd = normalize_domain(candidate)
        if nd is not None:
            out.add(nd)
            if len(out) >= _MAX_EXTERNAL_DOMAINS:
                break
    return frozenset(out)


class ExternalListCache:
    """Fetch + write-through disk cache of the external adult / bypass lists.

    Loads any prior disk cache on construction. :meth:`refresh_all` fetches both
    sources over HTTPS with size guards; on failure the stale/disk copy is kept.
    ``client_factory`` is injected so tests can supply an ``httpx.MockTransport``.
    """

    def __init__(
        self,
        cache_dir: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) / "webfilter_cache"
        self._client_factory = client_factory
        self._urls = {
            "adult": os.environ.get("KENNY_WEBFILTER_ADULT_URL", _DEFAULT_ADULT_URL),
            "bypass": os.environ.get("KENNY_WEBFILTER_BYPASS_URL", _DEFAULT_BYPASS_URL),
        }
        self._sets: dict[str, frozenset[str]] = {}
        self._last_fetch: dict[str, str | None] = {"adult": None, "bypass": None}
        self._warned: set[str] = set()
        self._load_disk()

    # -- disk cache --------------------------------------------------------

    def _disk_path(self, source: str) -> Path:
        return self._cache_dir / f"{source}.txt"

    def _load_disk(self) -> None:
        for source in _SOURCES:
            path = self._disk_path(source)
            if path.is_file():
                try:
                    self._sets[source] = _parse_list(path.read_text(encoding="utf-8"))
                except OSError as exc:  # pragma: no cover - unlikely
                    logger.warning("failed to read %s cache: %s", source, exc)

    def _write_disk(self, source: str, domains: frozenset[str]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._disk_path(source).write_text(
                "\n".join(sorted(domains)), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - unlikely
            logger.warning("failed to write %s cache: %s", source, exc)

    # -- fetching ----------------------------------------------------------

    def _make_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.AsyncClient()

    async def _fetch_one(self, source: str) -> None:
        url = self._urls[source]
        try:
            async with self._make_client() as client:
                resp = await client.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code != 200:
                logger.warning(
                    "webfilter %s fetch returned %s; keeping cached list",
                    source,
                    resp.status_code,
                )
                return
            body = resp.content
            if len(body) > _MAX_BODY_BYTES:
                logger.warning(
                    "webfilter %s body %d bytes > %d cap; rejected",
                    source,
                    len(body),
                    _MAX_BODY_BYTES,
                )
                return
            parsed = _parse_list(body.decode("utf-8", "replace"))
            self._sets[source] = parsed
            self._last_fetch[source] = datetime.now(timezone.utc).isoformat()
            self._write_disk(source, parsed)
            logger.info("webfilter %s list: %d domains", source, len(parsed))
        except Exception as exc:  # noqa: BLE001 - keep stale copy on any failure
            logger.warning("webfilter %s fetch failed: %s", source, exc)

    async def refresh_all(self) -> None:
        """Fetch both external sources (best-effort; stale copies kept on failure)."""

        for source in _SOURCES:
            await self._fetch_one(source)

    # -- accessors ---------------------------------------------------------

    def get(self, source: str) -> frozenset[str]:
        """Return the cached domain set for ``source`` (empty when never loaded)."""

        result = self._sets.get(source)
        if result is None:
            if source not in self._warned:
                logger.warning("webfilter %s list not fetched yet; using empty set", source)
                self._warned.add(source)
            return frozenset()
        return result

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per-source ``{count, last_fetch}`` for the dashboard."""

        return {
            source: {
                "count": len(self._sets.get(source, frozenset())),
                "last_fetch": self._last_fetch.get(source),
            }
            for source in _SOURCES
        }


# --- effective list / apply-args ----------------------------------------------

_HARD_CAP = 10_000  # agent hard cap; server must never exceed it.


def _max_block_domains() -> int:
    try:
        return int(os.environ.get("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS", "5000"))
    except ValueError:
        return 5000


def _add_block(blocks: dict[str, str], domain: str, category: str) -> None:
    existing = blocks.get(domain)
    if existing is None or _CATEGORY_PRIORITY[category] > _CATEGORY_PRIORITY[existing]:
        blocks[domain] = category


def effective_list(
    config: dict[str, Any],
    custom_rows: list[dict[str, Any]],
    cache: ExternalListCache,
) -> dict[str, Any]:
    """Build the per-host matchable list (for flagging).

    ``blocks`` maps each matchable domain to its category; ``allows`` is the set
    of custom ``allow`` entries. ``watch`` and ``block`` custom entries are both
    matchable (category ``custom``); only ``block`` is later enforced on the
    agent. Seed always contributes; external layers follow their config toggles.
    """

    blocks: dict[str, str] = {}
    allows: set[str] = set()

    for row in custom_rows:
        domain = row.get("domain")
        action = row.get("action")
        if not domain:
            continue
        if action == "allow":
            allows.add(domain)
        elif action in ("watch", "block"):
            _add_block(blocks, domain, "custom")

    for domain in load_seed():
        _add_block(blocks, domain, "seed")

    if config.get("use_external_adult"):
        for domain in cache.get("adult"):
            _add_block(blocks, domain, "external_adult")

    if config.get("use_bypass_protection"):
        for domain in cache.get("bypass"):
            _add_block(blocks, domain, "bypass")

    return {"blocks": blocks, "allows": allows}


def build_apply_args(
    config: dict[str, Any],
    custom_rows: list[dict[str, Any]],
    cache: ExternalListCache,
) -> dict[str, Any]:
    """Build the flat block set + hash pushed to the agent (``webfilter_apply``).

    Includes custom ``block`` entries, the seed, a capped external-adult extract
    (when enabled), and the bypass list (when enabled), minus ``allow`` entries.
    Sorted deterministically; ``list_hash`` is ``sha256(joined)[:16]``. Never
    exceeds the agent hard cap (10 000).
    """

    allows = {r["domain"] for r in custom_rows if r.get("action") == "allow"}
    domains: set[str] = {
        r["domain"] for r in custom_rows if r.get("action") == "block" and r.get("domain")
    }
    domains.update(load_seed())
    if config.get("use_external_adult"):
        adult = sorted(cache.get("adult"))[: _max_block_domains()]
        domains.update(adult)
    if config.get("use_bypass_protection"):
        domains.update(cache.get("bypass"))
    domains -= allows

    ordered = sorted(domains)
    if len(ordered) > _HARD_CAP:
        ordered = ordered[:_HARD_CAP]
    list_hash = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()[:16]
    doh_policy = config.get("doh_policy") or "disable"
    return {"domains": ordered, "doh_policy": doh_policy, "list_hash": list_hash}


# --- service facade -----------------------------------------------------------

_VALID_ACTIONS = ("watch", "block", "allow")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class WebFilterService:
    """Async facade over a :class:`WebFilterStore` + :class:`ExternalListCache`."""

    def __init__(self, store: WebFilterStore, cache: ExternalListCache) -> None:
        self.store = store
        self.cache = cache

    # -- config / list CRUD ------------------------------------------------

    async def get_config(self, agent_id: str) -> dict[str, Any]:
        return await self.store.get_config(agent_id)

    async def set_config(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        return await self.store.set_config(agent_id, **fields)

    async def list_domains(self, agent_id: str) -> list[dict[str, Any]]:
        return await self.store.list_domains(agent_id)

    async def add_domain(
        self, agent_id: str, domain: str, action: str, note: str | None = None
    ) -> str:
        nd = normalize_domain(domain)
        if nd is None:
            raise ValueError(f"invalid domain: {domain!r}")
        if action not in _VALID_ACTIONS:
            raise ValueError(f"action must be one of {_VALID_ACTIONS}")
        await self.store.add_domain(agent_id, nd, action, note)
        return nd

    async def remove_domain(self, agent_id: str, domain: str) -> bool:
        nd = normalize_domain(domain) or domain
        return await self.store.remove_domain(agent_id, nd)

    async def activity(
        self, agent_id: str, hours: int = 24, flagged_only: bool = False
    ) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return await self.store.activity(agent_id, since, flagged_only)

    # -- apply / state -----------------------------------------------------

    async def build_apply(self, agent_id: str) -> dict[str, Any]:
        config = await self.store.get_config(agent_id)
        rows = await self.store.list_domains(agent_id)
        return build_apply_args(config, rows, self.cache)

    async def current_list_hash(self, agent_id: str) -> str:
        return (await self.build_apply(agent_id))["list_hash"]

    async def set_applied_state(
        self, agent_id: str, list_hash: str | None, applied_at: str, ok: bool
    ) -> None:
        await self.store.set_applied_state(agent_id, list_hash, applied_at, ok)

    # -- insert-time enrichment -------------------------------------------

    async def record_activity(
        self, agent_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Upsert observed domains + annotate the payload with ``flagged``.

        Always records observed domains into ``web_activity_events``. When the
        feature is enabled for the host, returns a copy of ``payload`` with a
        ``flagged`` array (matched domains, category, timestamps) and
        ``flagged_count_24h``. When disabled, returns ``payload`` unchanged so the
        health rule defers (no ``flagged`` key).
        """

        config = await self.store.get_config(agent_id)
        rows = await self.store.list_domains(agent_id)
        enabled = bool(config.get("enabled"))
        effective = effective_list(config, rows, self.cache) if enabled else None

        observed = payload.get("domains") or []
        events: list[dict[str, Any]] = []
        flagged: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for item in observed:
            if not isinstance(item, dict):
                continue
            domain = normalize_domain(item.get("domain"))
            if domain is None:
                continue
            category: str | None = None
            matched_entry: str | None = None
            if effective is not None:
                hit = classify(domain, effective)
                if hit is not None:
                    category, matched_entry = hit
            first_seen = item.get("first_seen")
            last_seen = item.get("last_seen")
            sources = item.get("sources") or []
            events.append(
                {
                    "domain": domain,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "hits": int(item.get("hits") or 0),
                    "sources": [str(s) for s in sources],
                    "flagged": category is not None,
                    "category": category,
                }
            )
            if category is not None:
                flagged.append(
                    {
                        "domain": domain,
                        "category": category,
                        "matched_entry": matched_entry,
                        "first_seen": first_seen,
                        "last_seen": last_seen,
                    }
                )

        if events:
            await self.store.upsert_events(agent_id, events)

        if not enabled:
            return payload

        annotated = dict(payload)
        annotated["flagged"] = flagged
        cutoff = now - timedelta(hours=24)
        annotated["flagged_count_24h"] = sum(
            1
            for f in flagged
            if (ts := _parse_ts(f.get("last_seen"))) is not None
            and (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) >= cutoff
        )
        return annotated
