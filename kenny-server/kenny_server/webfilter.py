"""Parental-controls web filtering: matching, external lists, and the service facade.

This module owns the server-side half of the ``web_activity`` / ``webfilter_*``
feature (ADR-0024):

* :func:`normalize_domain` / :func:`matches` / :func:`classify` — the pure
  domain-matching core (suffix match, allow-precedence, layered categories).
* :func:`load_seed` — the shipped seed of well-known adult domains.
* :data:`CATEGORY_CATALOG` — the named category layers. An *external* category
  names a maintained upstream list fetched by :class:`ExternalListCache`; a
  *local* category has no upstream and gathers only the per-host custom entries
  tagged with it. Both are toggled per host and can be added for a window by
  the schedule.
* :class:`ExternalListCache` — fetch + write-through disk cache of every
  external category's list, with size guards and a disk fallback when offline.
* :func:`effective_list` / :func:`build_apply_args` — the per-host layered list
  used for matching (flagging) vs the flat block set pushed to the agent.
  :func:`build_apply_args` raises :class:`ListTooLargeError` rather than
  truncating past the agent's hard cap.
* :class:`ScheduleWindow` + :func:`schedule_state` — per-host weekday/time
  windows that add categories for their duration, evaluated in a named IANA
  timezone. The window model is entirely server-side: the agent has no clock
  and no concept of a category (ADR-0024, ADR-0055).
* :class:`WebFilterService` — the async facade the tunnel, API, and MCP tools
  use; wraps a :class:`~kenny_server.store.WebFilterStore` + an
  :class:`ExternalListCache`.

The server is the authoritative matcher; the agent is a dumb, idempotent
enforcer. Everything here — categories, the clock, the schedule — resolves to
the one unchanged payload the agent already understands:
``webfilter_apply({domains, doh_policy, list_hash})``. See ADR-0024 and
``docs/protocol.md`` for the contract shapes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# Provenance precedence when a domain is contributed by several layers: a custom
# entry always wins over the shipped seed, which wins over an external list.
# These are *provenance* labels shown next to a flagged hit, not the category
# taxonomy below — ``bypass`` sits last because "this host is trying to
# circumvent the filter" is the least informative reason to show a parent when
# a content list also matched.
_CATEGORY_PRIORITY = {"custom": 3, "seed": 2, "external_adult": 1, "bypass": 0}
_DEFAULT_PRIORITY = 1  # any other external category ranks with external_adult
Category = str  # a provenance label: "custom" | "seed" | a category's provenance


def _priority(label: str) -> int:
    return _CATEGORY_PRIORITY.get(label, _DEFAULT_PRIORITY)


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


# --- category catalog ---------------------------------------------------------

_DEFAULT_ADULT_URL = (
    "https://raw.githubusercontent.com/StevenBlack/hosts/master/"
    "alternates/porn-only/hosts"
)
_DEFAULT_BYPASS_URL = (
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/"
    "domains/doh-vpn-proxy-bypass.txt"
)
_DEFAULT_GAMBLING_URL = (
    "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/domains/gambling.txt"
)
_DEFAULT_PIRACY_URL = (
    "https://raw.githubusercontent.com/blocklistproject/Lists/master/piracy.txt"
)


@dataclass(frozen=True)
class CategorySpec:
    """One named category layer.

    ``key`` is the stable identifier stored in configs, schedule windows and on
    custom entries. ``provenance`` is the label :func:`classify` reports for a
    domain this layer contributed — deliberately decoupled from ``key`` so the
    adult category can keep reporting the historical ``external_adult`` label
    that already sits in stored ``web_activity_events`` rows.

    ``url`` is ``None`` for a **local** category: it has no upstream and
    contributes only the per-host custom entries tagged with it (a parent's own
    "social" or "gaming" list). ``setting_key`` names a
    :mod:`kenny_server.config` catalog entry when one exists, so an operator's
    live override wins over the environment; categories added after that catalog
    was written fall back to ``KENNY_WEBFILTER_<KEY>_URL`` or the default.

    ``capped`` marks a category whose external extract is subject to
    ``KENNY_WEBFILTER_MAX_BLOCK_DOMAINS``. Bypass protection is deliberately
    uncapped: it is the layer that stops the filter being circumvented, so
    silently dropping half of it would defeat its purpose.
    """

    key: str
    label: str
    provenance: str
    url: str | None = None
    setting_key: str | None = None
    capped: bool = True

    @property
    def external(self) -> bool:
        return self.url is not None

    @property
    def env_key(self) -> str:
        return f"KENNY_WEBFILTER_{self.key.upper()}_URL"


CATEGORY_CATALOG: dict[str, CategorySpec] = {
    spec.key: spec
    for spec in (
        CategorySpec(
            "adult", "Adult content", "external_adult",
            url=_DEFAULT_ADULT_URL, setting_key="KENNY_WEBFILTER_ADULT_URL",
        ),
        CategorySpec(
            "bypass", "VPN / proxy / DoH bypass", "bypass",
            url=_DEFAULT_BYPASS_URL, setting_key="KENNY_WEBFILTER_BYPASS_URL",
            capped=False,
        ),
        CategorySpec("gambling", "Gambling", "gambling", url=_DEFAULT_GAMBLING_URL),
        CategorySpec("piracy", "Piracy / torrents", "piracy", url=_DEFAULT_PIRACY_URL),
        CategorySpec("social", "Social networks", "social"),
        CategorySpec("gaming", "Gaming", "gaming"),
        CategorySpec("streaming", "Video / streaming", "streaming"),
        CategorySpec("shopping", "Shopping", "shopping"),
        CategorySpec("chat", "Messaging", "chat"),
    )
}

#: Every valid category key, in catalog order.
CATEGORY_KEYS: tuple[str, ...] = tuple(CATEGORY_CATALOG)

#: The two categories that predate the catalog and keep their own boolean
#: columns on ``webfilter_config`` (``use_external_adult`` /
#: ``use_bypass_protection``). Those columns stay the single source of truth for
#: these two so the existing API, dashboard and tests keep working; every other
#: category lives in the config's ``categories`` list. ``get_config`` merges
#: both into one canonical ``categories`` field.
LEGACY_TOGGLE_CATEGORIES: dict[str, str] = {
    "adult": "use_external_adult",
    "bypass": "use_bypass_protection",
}


def validate_category(key: Any) -> str:
    """Return ``key`` as a known category, or raise :class:`ValueError`."""

    name = str(key or "").strip().lower()
    if name not in CATEGORY_CATALOG:
        raise ValueError(f"unknown category {key!r}; known: {', '.join(CATEGORY_KEYS)}")
    return name


def validate_categories(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Normalize an iterable of category keys (deduped, catalog order)."""

    if values is None:
        return ()
    seen = {validate_category(v) for v in values}
    return tuple(k for k in CATEGORY_KEYS if k in seen)


def describe_categories() -> list[dict[str, Any]]:
    """The catalog as JSON for the dashboard's category picker."""

    return [
        {
            "key": spec.key,
            "label": spec.label,
            "external": spec.external,
            "capped": spec.capped,
        }
        for spec in CATEGORY_CATALOG.values()
    ]


def active_categories(
    config: dict[str, Any], extra: Iterable[str] = ()
) -> frozenset[str]:
    """The categories in force for a host right now.

    The host's own toggles (the two legacy booleans plus the ``categories``
    list) unioned with ``extra`` — the categories a currently-active schedule
    window adds. A schedule only ever *adds*, never removes: a window can make
    the filter stricter for its duration, never weaker.
    """

    base: set[str] = set()
    for key, column in LEGACY_TOGGLE_CATEGORIES.items():
        if config.get(column):
            base.add(key)
    for value in config.get("categories") or ():
        if value in CATEGORY_CATALOG:
            base.add(str(value))
    for value in extra:
        if value in CATEGORY_CATALOG:
            base.add(str(value))
    return frozenset(base)


# --- external lists -----------------------------------------------------------

# Guards against a compromised/oversized upstream (CWE-400/770).
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_EXTERNAL_DOMAINS = 300_000
# Sink IPs a hosts file uses; the domain sits in the second column after these.
_SINK_IPS = {"0.0.0.0", "127.0.0.1", "::1", "255.255.255.255"}

#: The sources the cache fetches: every external category in the catalog.
_SOURCES: tuple[str, ...] = tuple(
    key for key, spec in CATEGORY_CATALOG.items() if spec.external
)


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
    """Fetch + write-through disk cache of every external category's list.

    One entry per external category in :data:`CATEGORY_CATALOG`. Loads any prior
    disk cache on construction. :meth:`refresh_all` fetches every source over
    HTTPS with size guards; on failure the stale/disk copy is kept.
    ``client_factory`` is injected so tests can supply an ``httpx.MockTransport``.
    """

    def __init__(
        self,
        cache_dir: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        settings: Any = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) / "webfilter_cache"
        self._client_factory = client_factory
        # When ``settings`` is provided the source URLs and the block-domain cap
        # are read live from it (DB > env > default) on each fetch, so operator
        # changes apply on the next refresh. Otherwise fall back to env/default.
        self._settings = settings
        self._sets: dict[str, frozenset[str]] = {}
        self._last_fetch: dict[str, str | None] = dict.fromkeys(_SOURCES)
        self._warned: set[str] = set()
        self._load_disk()

    def _url(self, source: str) -> str:
        """Resolve one source's URL: live setting > env > coded default.

        Only the two categories that predate the settings catalog have a spec
        there; asking :class:`~kenny_server.config.Settings` for an unlisted key
        raises, so newer categories resolve through their own env key instead.
        """

        spec = CATEGORY_CATALOG[source]
        default = spec.url or ""
        if spec.setting_key is not None and self._settings is not None:
            return str(self._settings.get(spec.setting_key))
        return os.environ.get(spec.env_key, default)

    def max_block_domains(self) -> int:
        """Live cap on external adult domains pushed to an agent (hard-capped)."""

        if self._settings is not None:
            value = int(self._settings.get("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS"))
        else:
            value = _max_block_domains()
        return max(0, min(value, _HARD_CAP))

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
        url = self._url(source)
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


class ListTooLargeError(ValueError):
    """The effective block list would exceed the agent's hard cap.

    Raised instead of truncating. The agent rejects an over-cap list outright
    with ``bad_args`` — it does not truncate — so silently shipping a prefix
    would give the operator a filter they believe is complete and is not, and
    shipping the whole thing would give them a push that always fails. Both are
    worse than refusing server-side with a count and a way out.
    """

    def __init__(self, count: int, cap: int) -> None:
        self.count = count
        self.cap = cap
        super().__init__(
            f"effective block list is {count} domains, over the agent's {cap} cap "
            f"by {count - cap}; turn off a category, add allow entries, or lower "
            "KENNY_WEBFILTER_MAX_BLOCK_DOMAINS"
        )


def _max_block_domains() -> int:
    try:
        return int(os.environ.get("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS", "5000"))
    except ValueError:
        return 5000


def _add_block(blocks: dict[str, str], domain: str, category: str) -> None:
    existing = blocks.get(domain)
    if existing is None or _priority(category) > _priority(existing):
        blocks[domain] = category


def _row_applies(row: dict[str, Any], active: frozenset[str]) -> bool:
    """True when a custom entry is in force under ``active``.

    An untagged entry (``category`` NULL — every entry that predates categories)
    always applies. A tagged one applies only while its category is on, whether
    by the host's own toggles or by an active schedule window.
    """

    category = row.get("category")
    return not category or category in active


def effective_list(
    config: dict[str, Any],
    custom_rows: list[dict[str, Any]],
    cache: ExternalListCache,
    *,
    extra_categories: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the per-host matchable list (for flagging).

    ``blocks`` maps each matchable domain to its provenance label; ``allows`` is
    the set of in-force custom ``allow`` entries. ``watch`` and ``block`` custom
    entries are both matchable (provenance ``custom``); only ``block`` is later
    enforced on the agent. The seed always contributes; every other layer
    follows :func:`active_categories`, so a schedule window widens what is
    *flagged* exactly as it widens what is blocked.
    """

    active = active_categories(config, extra_categories)
    blocks: dict[str, str] = {}
    allows: set[str] = set()

    for row in custom_rows:
        domain = row.get("domain")
        action = row.get("action")
        if not domain or not _row_applies(row, active):
            continue
        if action == "allow":
            allows.add(domain)
        elif action in ("watch", "block"):
            _add_block(blocks, domain, "custom")

    for domain in load_seed():
        _add_block(blocks, domain, "seed")

    for key in active:
        spec = CATEGORY_CATALOG[key]
        if not spec.external:
            continue
        for domain in cache.get(key):
            _add_block(blocks, domain, spec.provenance)

    return {"blocks": blocks, "allows": allows}


def build_apply_args(
    config: dict[str, Any],
    custom_rows: list[dict[str, Any]],
    cache: ExternalListCache,
    *,
    extra_categories: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the flat block set + hash pushed to the agent (``webfilter_apply``).

    Includes the in-force custom ``block`` entries, the seed, a capped extract of
    every enabled *capped* external category, and every enabled uncapped one
    (bypass protection), minus in-force ``allow`` entries. Sorted
    deterministically; ``list_hash`` is ``sha256(joined)[:16]``.

    Raises :class:`ListTooLargeError` when the result would exceed the agent's
    hard cap of 10 000. The capped categories share one budget
    (``KENNY_WEBFILTER_MAX_BLOCK_DOMAINS``) rather than one each, so enabling a
    second content category does not silently double what is pushed.
    """

    active = active_categories(config, extra_categories)
    rows = [r for r in custom_rows if _row_applies(r, active)]
    allows = {r["domain"] for r in rows if r.get("action") == "allow"}
    domains: set[str] = {
        r["domain"] for r in rows if r.get("action") == "block" and r.get("domain")
    }
    domains.update(load_seed())

    capped: set[str] = set()
    for key in active:
        spec = CATEGORY_CATALOG[key]
        if not spec.external:
            continue
        if spec.capped:
            capped |= cache.get(key)
        else:
            domains |= cache.get(key)
    if capped:
        domains.update(sorted(capped)[: cache.max_block_domains()])
    domains -= allows

    ordered = sorted(domains)
    if len(ordered) > _HARD_CAP:
        raise ListTooLargeError(len(ordered), _HARD_CAP)
    list_hash = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()[:16]
    doh_policy = config.get("doh_policy") or "disable"
    return {"domains": ordered, "doh_policy": doh_policy, "list_hash": list_hash}


# --- bypass requests ----------------------------------------------------------

#: The ticket category a bypass request carries (``tickets.KNOWN_CATEGORIES``).
#: A bypass request *is* a ticket: the requester is the child, the existing
#: operator-approval gate is the decision, and granting it is the operator's
#: ordinary ``webfilter_set(add_domain, action="allow")`` + ``webfilter_push``.
#: Nothing here duplicates that lifecycle — this module only reads a ticket to
#: say which host and which domains it is about.
#:
#: Not to be confused with the ``bypass`` *category*, which blocks VPN/proxy/DoH
#: domains to stop a child circumventing the filter. That is the opposite
#: direction: one asks permission, the other refuses to be evaded.
BYPASS_REQUEST_CATEGORY = "web_filter"

# Bare hosts inside free text: at least two dot-separated labels, TLD-like tail.
_TEXT_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9¡-￿](?:[a-z0-9¡-￿-]*[a-z0-9¡-￿])?\.)+[a-z¡-￿]{2,})\b",
    re.IGNORECASE,
)

# A string built from many short "label." repeats that never resolves to a
# valid TLD tail (so the match ultimately fails) makes _TEXT_DOMAIN_RE's
# backtracking cost grow quadratically with input length (CWE-1333): the
# outer `+` must retry every rep count, and nothing bounds how large `title`/
# `summary` can be (ticket text is untrusted, see the docstring below). Cap
# what actually reaches the regex rather than trying to make the pattern
# itself immune to every pathological shape — same defensive idiom as this
# module's other input ceilings (`_MAX_BODY_BYTES`, `_MAX_EXTERNAL_DOMAINS`).
# Generous for a genuine mention of a domain in typed text.
_MAX_SCAN_CHARS = 1_000


def requested_domains(*texts: Any, limit: int = 10) -> list[str]:
    """Normalized domains mentioned in a bypass request's text.

    A convenience for showing the operator *what* is being asked for next to the
    ticket; it never decides anything. Ticket text is untrusted agent-adjacent
    input (ADR-0023), so candidates go through :func:`normalize_domain` like any
    other domain and the result is bounded. Each text is also truncated to
    :data:`_MAX_SCAN_CHARS` before scanning (see its docstring).
    """

    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for match in _TEXT_DOMAIN_RE.finditer(text[:_MAX_SCAN_CHARS]):
            nd = normalize_domain(match.group(1))
            if nd is None or nd in seen:
                continue
            seen.add(nd)
            out.append(nd)
            if len(out) >= limit:
                return out
    return out


# --- schedule -----------------------------------------------------------------

#: Weekday keys, Monday-first, matching ``datetime.weekday()`` ordering and the
#: spelling ``config._DAYS`` already uses for the weekly digest.
DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# How far ahead to look for the next schedule boundary. Windows recur weekly, so
# eight days always contains at least one occurrence of every enabled window.
_LOOKAHEAD_DAYS = 8

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def default_timezone() -> str:
    """The IANA zone new windows default to: the server's ``TZ``, else UTC."""

    name = (os.environ.get("TZ") or "").strip()
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("TZ=%r is not a known IANA zone; scheduling in UTC", name)
        return "UTC"
    return name


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return the :class:`ZoneInfo` for ``name``, or raise :class:`ValueError`."""

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def parse_days(value: Any) -> tuple[int, ...]:
    """Normalize weekdays to sorted ``0..6`` indices (Monday = 0).

    Accepts a comma-separated string (``"mon,tue"``), an iterable of day keys,
    or an iterable of ints. ``"daily"`` / ``"all"`` expand to the whole week.
    """

    if isinstance(value, str):
        parts: list[Any] = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise ValueError("days must be a list or a comma-separated string")
    out: set[int] = set()
    for part in parts:
        if isinstance(part, bool):
            raise ValueError(f"invalid weekday {part!r}")
        if isinstance(part, int):
            if not 0 <= part <= 6:
                raise ValueError(f"weekday index {part} out of range 0..6")
            out.add(part)
            continue
        key = str(part).strip().lower()[:5]
        if key in ("daily", "all"):
            out.update(range(7))
            continue
        short = key[:3]
        if short not in DAY_KEYS:
            raise ValueError(f"invalid weekday {part!r}; use {', '.join(DAY_KEYS)}")
        out.add(DAY_KEYS.index(short))
    if not out:
        raise ValueError("a window needs at least one weekday")
    return tuple(sorted(out))


def parse_hhmm(value: Any) -> int:
    """Parse ``"HH:MM"`` (or an int of minutes) into minutes since midnight."""

    if isinstance(value, bool):
        raise ValueError(f"invalid time {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 1439:
            raise ValueError(f"minute-of-day {value} out of range 0..1439")
        return value
    match = _HHMM_RE.match(str(value).strip())
    if match is None:
        raise ValueError(f"invalid time {value!r}; expected HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def format_hhmm(minutes: int) -> str:
    """Render minutes since midnight as ``"HH:MM"``."""

    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class ScheduleWindow:
    """One recurring per-host window that adds categories while it is open.

    ``start_min``/``end_min`` are minutes since local midnight in ``tz``.
    ``end_min <= start_min`` means the window wraps past midnight into the next
    day (21:00–07:00 on ``mon`` runs Monday evening into Tuesday morning), which
    is the shape most bedtime rules take.
    """

    id: str
    agent_id: str
    label: str
    days: tuple[int, ...]
    start_min: int
    end_min: int
    categories: tuple[str, ...]
    tz: str
    enabled: bool
    created_at: str

    @property
    def wraps_midnight(self) -> bool:
        return self.end_min <= self.start_min

    def as_dict(self) -> dict[str, Any]:
        """JSON shape for the API / MCP overview."""

        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "label": self.label,
            "days": list(self.days),
            "day_keys": [DAY_KEYS[d] for d in self.days],
            "start": format_hhmm(self.start_min),
            "end": format_hhmm(self.end_min),
            "wraps_midnight": self.wraps_midnight,
            "categories": list(self.categories),
            "timezone": self.tz,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }


def make_window(
    agent_id: str,
    *,
    days: Any,
    start: Any,
    end: Any,
    categories: Iterable[Any],
    label: str = "",
    tz: str | None = None,
    enabled: bool = True,
    window_id: str | None = None,
    created_at: str | None = None,
) -> ScheduleWindow:
    """Validate raw window input into a :class:`ScheduleWindow`.

    Raises :class:`ValueError` for an unknown weekday, category or timezone, a
    malformed time, or a zero-length window — none of which may reach the DB,
    because the schedule loop reads these rows unattended and a row it cannot
    interpret would either crash a pass or silently do nothing.
    """

    keys = validate_categories(categories)
    if not keys:
        raise ValueError("a window must name at least one category")
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)
    if start_min == end_min:
        raise ValueError("a window's start and end must differ")
    zone = (tz or default_timezone()).strip()
    resolve_timezone(zone)
    return ScheduleWindow(
        id=window_id or uuid.uuid4().hex[:12],
        agent_id=agent_id,
        label=str(label or "").strip()[:80],
        days=parse_days(days),
        start_min=start_min,
        end_min=end_min,
        categories=keys,
        tz=zone,
        enabled=bool(enabled),
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )


def window_from_row(row: dict[str, Any]) -> ScheduleWindow:
    """Rebuild a :class:`ScheduleWindow` from a ``WebFilterStore`` row.

    Rows were validated by :func:`make_window` on the way in, so this trusts
    their shape but still filters the category list through the catalog: a
    category removed from the catalog in a later release must degrade to "that
    layer contributes nothing" rather than break every pass of the loop.
    """

    return ScheduleWindow(
        id=str(row["id"]),
        agent_id=str(row["agent_id"]),
        label=str(row.get("label") or ""),
        days=tuple(int(d) for d in row["days"]),
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        categories=tuple(c for c in row["categories"] if c in CATEGORY_CATALOG),
        tz=str(row.get("tz") or "UTC"),
        enabled=bool(row.get("enabled", True)),
        created_at=str(row.get("created_at") or ""),
    )


def _window_occurrences(
    window: ScheduleWindow, around: datetime
) -> list[tuple[datetime, datetime]]:
    """Concrete ``[start, end)`` instants for ``window`` near ``around``.

    Boundaries are wall-clock in the window's own zone (a 21:00–07:00 window
    still ends at 07:00 local on the day the clocks change), so both edges are
    built from a local date + local time rather than by adding a duration.
    """

    zone = resolve_timezone(window.tz)
    local = around.astimezone(zone)
    start_time = time(window.start_min // 60, window.start_min % 60)
    end_time = time(window.end_min // 60, window.end_min % 60)
    spans: list[tuple[datetime, datetime]] = []
    for offset in range(-1, _LOOKAHEAD_DAYS + 1):
        day: date = local.date() + timedelta(days=offset)
        if day.weekday() not in window.days:
            continue
        start_dt = datetime.combine(day, start_time, tzinfo=zone)
        end_day = day + timedelta(days=1) if window.wraps_midnight else day
        end_dt = datetime.combine(end_day, end_time, tzinfo=zone)
        spans.append((start_dt, end_dt))
    return spans


def window_active_at(window: ScheduleWindow, at: datetime) -> bool:
    """True when ``window`` is open at instant ``at``."""

    if not window.enabled:
        return False
    return any(start <= at < end for start, end in _window_occurrences(window, at))


def _window_categories_at(
    windows: Sequence[ScheduleWindow], at: datetime
) -> tuple[frozenset[str], list[ScheduleWindow]]:
    open_windows = [w for w in windows if window_active_at(w, at)]
    keys: set[str] = set()
    for window in open_windows:
        keys.update(window.categories)
    return frozenset(keys), open_windows


def next_schedule_change(
    windows: Sequence[ScheduleWindow], at: datetime
) -> datetime | None:
    """The next instant the schedule's added-category set actually changes.

    Every window edge in the lookahead is a *candidate*; only the first one that
    evaluates to a different set is returned, so two overlapping windows naming
    the same category do not report a change when one hands over to the other.
    """

    current, _open = _window_categories_at(windows, at)
    edges: set[datetime] = set()
    for window in windows:
        if not window.enabled:
            continue
        for start, end in _window_occurrences(window, at):
            for edge in (start, end):
                if edge > at:
                    edges.add(edge)
    for edge in sorted(edges):
        if _window_categories_at(windows, edge)[0] != current:
            return edge
    return None


def schedule_state(
    config: dict[str, Any],
    windows: Sequence[ScheduleWindow],
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    """The observable state of a host's schedule right now.

    Answers, in one payload, the two questions an operator looking at a host
    has: *is the list currently the stricter one, and when does it revert?*
    ``stricter`` is true whenever an open window is adding a category the host's
    own toggles do not already have; ``reverts_at`` is the instant that stops
    being true. Both are ``None``/false when nothing is scheduled.
    """

    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    enabled_windows = [w for w in windows if w.enabled]
    added, open_windows = _window_categories_at(enabled_windows, now)
    base = active_categories(config)
    effective = base | added
    extra = effective - base
    change = next_schedule_change(enabled_windows, now)
    zone = enabled_windows[0].tz if enabled_windows else default_timezone()
    tzinfo = resolve_timezone(zone)
    # Instants are reported in UTC so a client never has to guess which zone a
    # timestamp is in, with a local rendering alongside for display — the
    # operator thinks in "reverts at 07:00", the API answers in one zone.
    change_utc = change.astimezone(timezone.utc) if change else None
    return {
        "now": now.isoformat(),
        "timezone": zone,
        "local_now": now.astimezone(tzinfo).isoformat(),
        "base_categories": sorted(base),
        "extra_categories": sorted(extra),
        "effective_categories": sorted(effective),
        "active_windows": [w.as_dict() for w in open_windows],
        "stricter": bool(extra),
        "next_change_at": change_utc.isoformat() if change_utc else None,
        "next_change_local": change.astimezone(tzinfo).isoformat() if change else None,
        "reverts_at": change_utc.isoformat() if (change_utc and extra) else None,
        "windows": [w.as_dict() for w in windows],
    }


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
        self,
        agent_id: str,
        domain: str,
        action: str,
        note: str | None = None,
        category: str | None = None,
    ) -> str:
        nd = normalize_domain(domain)
        if nd is None:
            raise ValueError(f"invalid domain: {domain!r}")
        if action not in _VALID_ACTIONS:
            raise ValueError(f"action must be one of {_VALID_ACTIONS}")
        key = validate_category(category) if category else None
        await self.store.add_domain(agent_id, nd, action, note, key)
        return nd

    async def remove_domain(self, agent_id: str, domain: str) -> bool:
        nd = normalize_domain(domain) or domain
        return await self.store.remove_domain(agent_id, nd)

    async def activity(
        self, agent_id: str, hours: int = 24, flagged_only: bool = False
    ) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return await self.store.activity(agent_id, since, flagged_only)

    # -- schedule ----------------------------------------------------------

    async def list_windows(self, agent_id: str) -> list[ScheduleWindow]:
        rows = await self.store.list_windows(agent_id)
        return [window_from_row(r) for r in rows]

    async def add_window(self, agent_id: str, **fields: Any) -> ScheduleWindow:
        """Validate and persist one window. Raises ``ValueError`` on bad input."""

        window = make_window(agent_id, **fields)
        await self.store.add_window(
            {
                "id": window.id,
                "agent_id": window.agent_id,
                "label": window.label,
                "days": list(window.days),
                "start_min": window.start_min,
                "end_min": window.end_min,
                "categories": list(window.categories),
                "tz": window.tz,
                "enabled": window.enabled,
                "created_at": window.created_at,
            }
        )
        return window

    async def set_window_enabled(
        self, agent_id: str, window_id: str, enabled: bool
    ) -> bool:
        return await self.store.set_window_enabled(agent_id, window_id, enabled)

    async def remove_window(self, agent_id: str, window_id: str) -> bool:
        return await self.store.remove_window(agent_id, window_id)

    async def schedule_state(
        self, agent_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        """The host's observable schedule state (see :func:`schedule_state`)."""

        config = await self.store.get_config(agent_id)
        windows = await self.list_windows(agent_id)
        return schedule_state(config, windows, at=at)

    async def extra_categories(
        self, agent_id: str, *, at: datetime | None = None
    ) -> tuple[str, ...]:
        """Categories a currently-open window adds beyond the host's own toggles."""

        state = await self.schedule_state(agent_id, at=at)
        return tuple(state["extra_categories"])

    # -- apply / state -----------------------------------------------------

    async def build_apply(
        self, agent_id: str, *, at: datetime | None = None
    ) -> dict[str, Any]:
        """The ``webfilter_apply`` args for this host *at this instant*.

        The schedule is resolved here, so every caller — the manual push, the
        dashboard, the MCP tool and the background loop — pushes the same list
        for the same moment. The payload's shape is unchanged: the agent still
        receives one flat ``domains`` list and knows nothing about categories or
        the clock. Raises :class:`ListTooLargeError` past the agent's cap.
        """

        config = await self.store.get_config(agent_id)
        rows = await self.store.list_domains(agent_id)
        windows = await self.list_windows(agent_id)
        extra = schedule_state(config, windows, at=at)["extra_categories"]
        return build_apply_args(config, rows, self.cache, extra_categories=extra)

    async def current_list_hash(self, agent_id: str) -> str:
        return (await self.build_apply(agent_id))["list_hash"]

    async def schedule_due(
        self, *, at: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Hosts whose scheduled list differs from what they last had applied.

        One entry per host with an enabled window whose feature *and* block mode
        are on and whose freshly computed ``list_hash`` differs from the stored
        ``applied_hash``: ``{agent_id, args}`` when there is something to push,
        or ``{agent_id, error}`` when the list cannot be built (over the agent's
        cap). Hosts with no enabled window are never returned — authoring a
        window is what opts a host into unattended pushes (ADR-0055).

        Pure computation with no side effects: the caller owns the tunnel, the
        audit record and the applied-state write, which keeps this testable
        without an agent.
        """

        due: list[dict[str, Any]] = []
        for agent_id in await self.store.agents_with_windows():
            config = await self.store.get_config(agent_id)
            if not (config.get("enabled") and config.get("block_mode")):
                continue
            try:
                args = await self.build_apply(agent_id, at=at)
            except ListTooLargeError as exc:
                due.append({"agent_id": agent_id, "args": None, "error": str(exc)})
                continue
            if args["list_hash"] == config.get("applied_hash"):
                continue
            due.append({"agent_id": agent_id, "args": args, "error": None})
        return due

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
        effective = None
        if enabled:
            windows = await self.list_windows(agent_id)
            # Match against what is in force *now*, so a window that widened the
            # filter also widens what the parent is alarmed about — and, because
            # matching never consults the cap, an over-cap list that cannot be
            # pushed still raises the alarm.
            extra = schedule_state(config, windows)["extra_categories"]
            effective = effective_list(config, rows, self.cache, extra_categories=extra)

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
