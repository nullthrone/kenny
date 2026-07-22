"""Operator dashboard: static page + JSON API routes.

The dashboard is a single vanilla-JS page (``index.html``) that calls the
``/api/*`` routes built in :func:`build_api_routes`. Keep it dependency-light.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import PROTOCOL_VERSION, __version__, agent_release, changelog
from ..config import CATALOG, SettingNotWritable, Settings
from ..chat import (
    ChatExecutor,
    ChatSessions,
    confirm_pending,
    confirm_pending_events,
    persist_session,
    public_transcript,
    run_turn,
    run_turn_events,
)
from ..policy import PolicyEngine
from ..event_categories import annotate_snapshots
from ..forecast import build_facts, deterministic_summary, forecast_events
from ..recommend import ai_available, recommend_events, warning_facts
from ..registry import AgentRegistry
from ..store import ChatHistoryStore, EventStore, PolicyStore, TelemetryStore
from ..tokenstore import AgentTokenStore
from ..tools import CallLog, ScreenshotStore, build_health
from ..tunnel import AgentTunnel, ToolError
from ..webfilter import WebFilterService, load_seed, normalize_domain
from .authz import guard, principal_of, visible_ids

_INDEX = Path(__file__).parent / "index.html"
_ASSETS = Path(__file__).parent / "assets"
# Whitelist of static assets the dashboard loads via <link>/<img>/<script>.
# Kept explicit (no directory walk) so the route can't serve anything else.
# ``.js`` covers the vendored charting library (Apache ECharts) used by the
# Overview dashboard — bundled locally so the UI never reaches for a CDN.
_ASSET_TYPES = {
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".js": "application/javascript",
}


def build_api_routes(
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
    screenshots: ScreenshotStore,
    event_store: EventStore,
    token_store: AgentTokenStore | None = None,
    policy_store: PolicyStore | None = None,
    policy_engine: PolicyEngine | None = None,
    webfilter: WebFilterService | None = None,
    settings: Settings | None = None,
    user_store: Any = None,
    key_store: Any = None,
    alert_state: Any = None,
    webfilter_store: Any = None,
    client_factory: Any = None,
) -> list[Route]:
    """Build the dashboard's static + JSON routes.

    ``client_factory`` builds the Anthropic client for read-path event
    categorization; defaults to :func:`_anthropic_client` (injected in tests).
    """

    _APPLIES_TO = {"powershell", "self_protection", "path"}
    _WEBFILTER_ACTIONS = {"watch", "block", "allow"}

    async def index(_request: Request) -> FileResponse:
        return FileResponse(_INDEX)

    async def asset(request: Request) -> Response:
        """Serve a whitelisted brand asset (logo, favicon) for the dashboard.

        Resolved by basename only — no path traversal, no directory listing.
        """

        name = request.path_params["name"]
        path = (_ASSETS / name).resolve()
        media = _ASSET_TYPES.get(path.suffix.lower())
        if media is None or path.parent != _ASSETS.resolve() or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type=media)

    async def _annotate_reliability(snapshots: list[dict[str, Any] | None]) -> None:
        """Stamp category/severity/suspected_cause onto every reliability event
        across the given snapshots (mutating the in-memory copies loaded from the
        store). Thin wrapper around :func:`event_categories.annotate_snapshots`
        using this route module's injected ``client_factory`` (ADR-0028).
        """

        await annotate_snapshots(snapshots, client_factory=client_factory or _anthropic_client)

    async def api_fleet(request: Request) -> JSONResponse:
        ids = await _known_ids(registry, store)
        principal = principal_of(request)
        if principal is not None:
            ids = visible_ids(principal, ids)
        agents = [await _overview(i, registry, store) for i in ids]
        from .. import health_rules

        overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
        return JSONResponse({"overall": overall or "unknown", "agents": agents})

    async def api_fleet_overview(request: Request) -> JSONResponse:
        """Fleet-wide aggregates for the high-level Overview dashboard.

        Loads the latest snapshot + rolled-up health for every agent and hands
        them to :func:`fleet_stats.aggregate_overview`. Read-only; a ``user``-role
        caller only sees their assigned hosts.
        """

        from datetime import datetime, timedelta, timezone

        from .. import fleet_stats, trends

        ids = await _known_ids(registry, store)
        principal = principal_of(request)
        if principal is not None:
            ids = visible_ids(principal, ids)
        forecast_since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        snapshots = []
        rows = []
        for agent_id in ids:
            agent = registry.get(agent_id)
            latest = await store.latest(agent_id)
            snapshot = latest["snapshot"] if latest else None
            snapshots.append(snapshot)
            rows.append((agent_id, agent, snapshot, latest))
        # Annotate reliability events with friendly categories before health
        # evaluation + aggregation, so both the reason and the heatmap use them.
        await _annotate_reliability(snapshots)
        agents: list[dict[str, Any]] = [
            {
                "agent_id": agent_id,
                "online": bool(agent and agent.online),
                "os": agent.os if agent else "windows",
                "meta": agent.meta if agent else {},
                "snapshot": snapshot,
                "health": build_health(snapshot, agent_os=agent.os if agent else "windows"),
                "collected_at": latest["collected_at"] if latest else None,
            }
            for agent_id, agent, snapshot, latest in rows
        ]
        disk_forecasts: dict[str, list[dict[str, Any]]] = {}
        for agent_id in ids:
            daily = await store.daily_latest(agent_id, forecast_since)
            disk_forecasts[agent_id] = trends.disk_forecast(daily)
        return JSONResponse(
            fleet_stats.aggregate_overview(agents, disk_forecasts=disk_forecasts)
        )

    async def api_fleet_trend(request: Request) -> JSONResponse:
        """Daily fleet health counts over a window (default 30 days, capped 1–90)."""

        from datetime import datetime, timedelta, timezone

        from .. import fleet_stats

        try:
            days = int(request.query_params.get("days", 30))
        except ValueError:
            days = 30
        days = max(1, min(days, 90))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

        ids = await _known_ids(registry, store)
        principal = principal_of(request)
        if principal is not None:
            ids = visible_ids(principal, ids)
        points_by_agent: dict[str, list[dict[str, Any]]] = {}
        for agent_id in ids:
            agent = registry.get(agent_id)
            agent_os = agent.os if agent else "windows"
            daily = await store.daily_latest(agent_id, since)
            points_by_agent[agent_id] = [
                {
                    "collected_at": d["collected_at"],
                    "overall": build_health(d["snapshot"], agent_os=agent_os)["overall"],
                }
                for d in daily
            ]
        return JSONResponse(fleet_stats.aggregate_trend(points_by_agent, days))

    async def api_agent(request: Request) -> JSONResponse:
        agent_id = request.path_params["id"]
        agent = registry.get(agent_id)
        latest = await store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        history = await store.history(agent_id, limit=50)
        # Categorize the latest reliability events (for the detail heatmap + the
        # health reason). History points only carry `overall`, so they don't need it.
        await _annotate_reliability([snapshot])
        agent_os = agent.os if agent else "windows"
        hist_points = [
            {
                "collected_at": h["collected_at"],
                "overall": build_health(h["snapshot"], agent_os=agent_os)["overall"],
            }
            for h in history
        ]
        return JSONResponse(
            {
                "agent_id": agent_id,
                "online": bool(agent and agent.online),
                "os": agent_os,
                "meta": agent.meta if agent else {},
                "collected_at": latest["collected_at"] if latest else None,
                "snapshot": snapshot,
                "health": build_health(snapshot, agent_os=agent_os),
                # Whether the AI Recommendation block is offered for flagged
                # sections (true only when an Anthropic API key is configured).
                "ai_enabled": ai_available(),
                "history": hist_points,
                "call_log": [
                    c for c in await call_log.list() if c["agent_id"] == agent_id
                ],
            }
        )

    async def api_agent_changes(request: Request) -> JSONResponse:
        """Inventory changes between a ~N-day-old baseline snapshot and now (ADR-0030)."""

        from datetime import datetime, timedelta, timezone

        from .. import diffs

        agent_id = request.path_params["id"]
        try:
            days = int(request.query_params.get("days", 1))
        except ValueError:
            days = 1
        days = max(1, min(days, 30))
        latest = await store.latest(agent_id)
        if latest is None:
            return JSONResponse(
                {"agent_id": agent_id, "days": days, "baseline": None, "latest": None, "changes": []}
            )
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        daily = await store.daily_latest(agent_id, since)
        baseline = daily[0] if daily else None
        changes = (
            diffs.diff_snapshots(baseline["snapshot"], latest["snapshot"]) if baseline else []
        )
        return JSONResponse(
            {
                "agent_id": agent_id,
                "days": days,
                "baseline": baseline["collected_at"] if baseline else None,
                "latest": latest["collected_at"],
                "changes": changes,
            }
        )

    async def api_agent_trends(request: Request) -> JSONResponse:
        """Disk-full forecast and battery trend over the 30-day daily history."""

        from datetime import datetime, timedelta, timezone

        from .. import trends

        agent_id = request.path_params["id"]
        since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        daily = await store.daily_latest(agent_id, since)
        return JSONResponse(
            {
                "agent_id": agent_id,
                "disk": trends.disk_forecast(daily),
                "battery": trends.battery_trend(daily),
            }
        )

    async def api_digest_preview(request: Request) -> JSONResponse:
        """Render (but do not send) the weekly digest for a manual check."""

        from ..digest import build_digest

        title, body = await build_digest(store, event_store, registry)
        return JSONResponse({"title": title, "body": body})

    async def api_refresh(request: Request) -> JSONResponse:
        agent_id = request.path_params["id"]
        try:
            result = await tunnel.send_request(agent_id, "telemetry_collect", {}, 60)
            await call_log.record(agent_id, "telemetry_collect", {}, ok=True)
        except (ToolError, Exception) as exc:  # noqa: BLE001 - surface to UI
            message = exc.message if isinstance(exc, ToolError) else str(exc)
            await call_log.record(agent_id, "telemetry_collect", {}, ok=False, error=message)
            return JSONResponse({"ok": False, "error": message}, status_code=502)
        # Store the freshly collected snapshot so the drill-down updates.
        if result:
            from datetime import datetime, timezone

            await store.insert(agent_id, datetime.now(timezone.utc).isoformat(), result)
        return JSONResponse({"ok": True})

    async def api_screenshot(request: Request) -> Response:
        """Return the latest stored screenshot for an agent as a PNG (or 404)."""

        agent_id = request.path_params["id"]
        rec = screenshots.get(agent_id)
        if rec is None:
            return Response(status_code=404)
        return Response(content=base64.b64decode(rec["image_b64"]), media_type="image/png")

    async def api_capture(request: Request) -> JSONResponse:
        """Trigger a fresh screen capture via the tunnel and store the result."""

        agent_id = request.path_params["id"]
        try:
            result = await tunnel.send_request(agent_id, "screen_capture", {}, 30)
            await call_log.record(agent_id, "screen_capture", {}, ok=True)
        except (ToolError, Exception) as exc:  # noqa: BLE001 - surface to UI
            message = exc.message if isinstance(exc, ToolError) else str(exc)
            await call_log.record(agent_id, "screen_capture", {}, ok=False, error=message)
            return JSONResponse({"ok": False, "error": message}, status_code=502)
        if isinstance(result, dict) and "image_b64" in result:
            screenshots.put(agent_id, result["image_b64"], result.get("format", "png"))
        return JSONResponse({"ok": True})

    async def api_remotehelp(request: Request) -> JSONResponse:
        """Open Quick Assist on the agent's desktop for a remote-help session.

        Forwards ``remotehelp_start``; the returned ``note`` reminds the operator of
        the human-in-the-loop steps (helper shares the code, the person accepts).
        """

        agent_id = request.path_params["id"]
        try:
            result = await tunnel.send_request(agent_id, "remotehelp_start", {}, 30)
            await call_log.record(agent_id, "remotehelp_start", {}, ok=True)
        except (ToolError, Exception) as exc:  # noqa: BLE001 - surface to UI
            message = exc.message if isinstance(exc, ToolError) else str(exc)
            await call_log.record(agent_id, "remotehelp_start", {}, ok=False, error=message)
            return JSONResponse({"ok": False, "error": message}, status_code=502)
        note = result.get("note") if isinstance(result, dict) else None
        return JSONResponse({"ok": True, "note": note})

    async def api_audit(request: Request) -> JSONResponse:
        """Recent tool-call audit log across the fleet (for the dashboard).

        Each entry is annotated ``state_changing`` (vs read-only) so the UI can
        label confirm-gated calls without re-deriving the classification. A
        ``user``-role caller only sees entries for their assigned hosts.
        """

        from ..chat import is_state_changing

        principal = principal_of(request)
        entries = [
            {
                "at": c["at"],
                "agent_id": c["agent_id"],
                "tool": c["tool"],
                "ok": c["ok"],
                "error": c.get("error"),
                "state_changing": is_state_changing(c["tool"]),
            }
            for c in await call_log.list()
            if principal is None or principal.may_see(c["agent_id"])
        ]
        return JSONResponse({"entries": entries})

    async def api_events(request: Request) -> JSONResponse:
        """Fleet-wide log/audit events for the dashboard, newest-first.

        Query params: ``agent`` (agent_id), ``level``, ``kind`` (log|audit),
        and ``limit`` (int, default 200, capped at 500).
        """

        params = request.query_params
        agent = params.get("agent") or None
        level = params.get("level") or None
        kind = params.get("kind") or None
        try:
            limit = int(params.get("limit", 200))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))
        principal = principal_of(request)
        # A scoped user may not read events for hosts outside their scope. If they
        # filter to a specific host, enforce it; otherwise restrict the whole set.
        if principal is not None and principal.scoped:
            if agent is not None and not principal.may_see(agent):
                return JSONResponse(
                    {"error": "forbidden", "detail": "host not in your scope"},
                    status_code=403,
                )
        entries = await event_store.query(
            agent_id=agent, level=level, kind=kind, limit=limit
        )
        if principal is not None and principal.scoped:
            entries = [e for e in entries if principal.may_see(e.get("agent_id"))]
        return JSONResponse({"entries": entries})

    async def api_rotate_token(request: Request) -> JSONResponse:
        """Mint (or rotate) a per-agent token. Inherits /api operator auth.

        Returns ``{token: <plaintext once>}``; the plaintext is not stored and
        cannot be retrieved again. This is the entry point the installer-download
        workstream calls to provision an agent.
        """

        if token_store is None:
            return JSONResponse({"error": "token store not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        token = await token_store.create_or_rotate(agent_id)
        return JSONResponse({"agent_id": agent_id, "token": token})

    async def api_remove_host(request: Request) -> JSONResponse:
        """Remove a host from inventory: purge all of its data (ADR-0037).

        Operator+ only (the route guard enforces this); a ``user`` role can never
        reach it. Refuses hosts pinned via ``KENNY_AGENT_TOKENS`` since they would
        be re-seeded on the next restart.
        """

        from .. import inventory

        agent_id = request.path_params["id"]
        if inventory.seeded_in_env(agent_id):
            return JSONResponse(
                {
                    "error": "seeded",
                    "detail": (
                        "host is pinned in KENNY_AGENT_TOKENS; remove it there first"
                    ),
                },
                status_code=409,
            )
        if None in (user_store, key_store, alert_state, webfilter_store):
            return JSONResponse(
                {"error": "unavailable", "detail": "inventory stores not configured"},
                status_code=503,
            )
        result = await inventory.purge_agent(
            agent_id,
            registry=registry,
            store=store,
            event_store=event_store,
            alert_state=alert_state,
            token_store=token_store,
            key_store=key_store,
            webfilter_store=webfilter_store,
            user_store=user_store,
            screenshots=screenshots,
        )
        await call_log.record(agent_id, "remove_host", {}, ok=True)
        return JSONResponse({"ok": True, "agent_id": agent_id, "purged": result})

    async def api_policy_list(_request: Request) -> JSONResponse:
        """Built-in (catalog) + operator deny rules for the policy view (ADR-0021)."""

        builtin = policy_engine.builtin_rules() if policy_engine is not None else []
        operator = await policy_store.list() if policy_store is not None else []
        return JSONResponse({"builtin": builtin, "operator": operator})

    async def api_policy_add(request: Request) -> JSONResponse:
        """Append an operator deny rule, recompile the mirror, and broadcast it."""

        if policy_store is None:
            return JSONResponse({"error": "policy store not configured"}, status_code=503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rule_id = str(body.get("id", "")).strip()
        applies_to = str(body.get("applies_to", "")).strip()
        pattern = body.get("pattern", "")
        reason = str(body.get("reason", "")).strip()
        if not rule_id:
            return JSONResponse({"error": "id is required"}, status_code=400)
        if applies_to not in _APPLIES_TO:
            return JSONResponse(
                {"error": f"applies_to must be one of {sorted(_APPLIES_TO)}"},
                status_code=400,
            )
        if not isinstance(pattern, str) or not pattern:
            return JSONResponse({"error": "pattern is required"}, status_code=400)
        try:
            re.compile(pattern)
        except re.error as exc:
            return JSONResponse({"error": f"invalid pattern: {exc}"}, status_code=400)
        if not reason:
            return JSONResponse({"error": "reason is required"}, status_code=400)
        await policy_store.add(
            id=rule_id, applies_to=applies_to, pattern=pattern, reason=reason
        )
        operator = await policy_store.list()
        if policy_engine is not None:
            policy_engine.set_operator_rules(operator)
        await tunnel.broadcast_policy()
        return JSONResponse({"operator": operator})

    async def api_policy_remove(request: Request) -> JSONResponse:
        """Remove one operator deny rule, recompile the mirror, and broadcast."""

        if policy_store is None:
            return JSONResponse({"error": "policy store not configured"}, status_code=503)
        rule_id = request.path_params["id"]
        removed = await policy_store.remove(rule_id)
        operator = await policy_store.list()
        if policy_engine is not None:
            policy_engine.set_operator_rules(operator)
        await tunnel.broadcast_policy()
        return JSONResponse({"ok": True, "removed": removed, "operator": operator})

    # -- runtime settings --------------------------------------------------

    async def api_settings_list(_request: Request) -> JSONResponse:
        """Grouped catalog with effective values + source badges for the UI."""

        if settings is None:
            return JSONResponse({"error": "settings not configured"}, status_code=503)
        return JSONResponse({"groups": settings.describe()})

    async def api_settings_set(request: Request) -> JSONResponse:
        """Set one override. 400 unknown/invalid, 403 env-only, else apply."""

        if settings is None:
            return JSONResponse({"error": "settings not configured"}, status_code=503)
        key = request.path_params["key"]
        spec = CATALOG.get(key)
        if spec is None:
            return JSONResponse({"error": f"unknown setting {key}"}, status_code=400)
        if not spec.writable:
            return JSONResponse(
                {"error": f"{key} is managed via the environment"}, status_code=403
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if "value" not in body:
            return JSONResponse({"error": "value is required"}, status_code=400)
        raw = "" if body["value"] is None else str(body["value"])
        try:
            await settings.set(key, raw)
        except SettingNotWritable as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(settings.describe_one(key))

    async def api_settings_reset(request: Request) -> JSONResponse:
        """Drop an override so the key falls back to env/default."""

        if settings is None:
            return JSONResponse({"error": "settings not configured"}, status_code=503)
        key = request.path_params["key"]
        spec = CATALOG.get(key)
        if spec is None:
            return JSONResponse({"error": f"unknown setting {key}"}, status_code=400)
        if not spec.writable:
            return JSONResponse(
                {"error": f"{key} is managed via the environment"}, status_code=403
            )
        await settings.reset(key)
        return JSONResponse(settings.describe_one(key))

    # -- parental controls (webfilter) ------------------------------------

    async def _webfilter_overview(agent_id: str) -> dict[str, Any]:
        config = await webfilter.get_config(agent_id)
        custom = await webfilter.list_domains(agent_id)
        current_hash = await webfilter.current_list_hash(agent_id)
        applied_hash = config.get("applied_hash")
        stats = webfilter.cache.stats()
        return {
            "agent_id": agent_id,
            "config": config,
            "custom": custom,
            "seed_count": len(load_seed()),
            "external": {
                "adult": {**stats["adult"], "enabled": config["use_external_adult"]},
                "bypass": {**stats["bypass"], "enabled": config["use_bypass_protection"]},
            },
            "applied": {
                "hash": applied_hash,
                "at": config.get("applied_at"),
                "ok": config.get("applied_ok"),
            },
            "current_hash": current_hash,
            "drift": bool(applied_hash) and applied_hash != current_hash,
        }

    async def api_webfilter_get(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        return JSONResponse(await _webfilter_overview(request.path_params["id"]))

    async def api_webfilter_config(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        doh = body.get("doh_policy")
        if doh is not None and doh not in ("disable", "leave"):
            return JSONResponse(
                {"error": "doh_policy must be 'disable' or 'leave'"}, status_code=400
            )
        config = await webfilter.set_config(
            agent_id,
            enabled=body.get("enabled"),
            block_mode=body.get("block_mode"),
            use_external_adult=body.get("use_external_adult"),
            use_bypass_protection=body.get("use_bypass_protection"),
            doh_policy=doh,
        )
        return JSONResponse({"config": config})

    async def api_webfilter_add_domain(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = str(body.get("action", "block"))
        if action not in _WEBFILTER_ACTIONS:
            return JSONResponse(
                {"error": f"action must be one of {sorted(_WEBFILTER_ACTIONS)}"},
                status_code=400,
            )
        if normalize_domain(body.get("domain")) is None:
            return JSONResponse({"error": "invalid domain"}, status_code=400)
        note = body.get("note")
        domain = await webfilter.add_domain(agent_id, str(body["domain"]), action, note)
        return JSONResponse(
            {"domain": domain, "custom": await webfilter.list_domains(agent_id)}
        )

    async def api_webfilter_remove_domain(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        removed = await webfilter.remove_domain(agent_id, request.path_params["domain"])
        return JSONResponse(
            {"ok": True, "removed": removed, "custom": await webfilter.list_domains(agent_id)}
        )

    async def api_webfilter_apply(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        config = await webfilter.get_config(agent_id)
        args = await webfilter.build_apply(agent_id)
        block_mode = bool(config["block_mode"])
        tool = "webfilter_apply" if block_mode else "webfilter_clear"
        call_args: dict[str, Any] = args if block_mode else {}
        try:
            result = await tunnel.send_request(agent_id, tool, call_args, 30)
            await call_log.record(agent_id, tool, call_args, ok=True)
        except ToolError as exc:
            await call_log.record(agent_id, tool, call_args, ok=False, error=exc.message)
            # The kill switch refuses mutating tools with `disabled`; surface it
            # distinctly so the UI can show the local-override message (ADR-0026).
            if exc.code == "disabled":
                return JSONResponse({"ok": False, "error": "disabled"}, status_code=200)
            return JSONResponse({"ok": False, "error": exc.message}, status_code=502)
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            await call_log.record(agent_id, tool, call_args, ok=False, error=str(exc))
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        from datetime import datetime, timezone

        applied_at = str(result.get("applied_at") or datetime.now(timezone.utc).isoformat())
        await webfilter.set_applied_state(
            agent_id,
            args["list_hash"] if block_mode else None,
            applied_at,
            bool(result.get("ok", True)),
        )
        return JSONResponse(
            {"ok": True, "result": result, "applied": call_args, "block_mode": block_mode}
        )

    async def api_webfilter_activity(request: Request) -> JSONResponse:
        if webfilter is None:
            return JSONResponse({"error": "webfilter not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        params = request.query_params
        try:
            hours = int(params.get("hours", 24))
        except ValueError:
            hours = 24
        hours = max(1, min(hours, 24 * 30))
        flagged_only = params.get("flagged") in ("1", "true", "yes")
        events = await webfilter.activity(agent_id, hours=hours, flagged_only=flagged_only)
        return JSONResponse({"agent_id": agent_id, "hours": hours, "events": events})

    async def api_about(_request: Request) -> JSONResponse:
        """Static server identity for the About modal (no network)."""

        return JSONResponse(
            {
                "server_version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "repo": agent_release.github_repo(),
            }
        )

    async def api_changelog(_request: Request) -> JSONResponse:
        """GitHub Releases for the About modal's changelog, server-proxied + cached."""

        repo = agent_release.github_repo()
        releases = await changelog.fetch_releases(repo)
        return JSONResponse({"repo": repo, "releases": releases})

    # Role/scope policy (ADR-0037), enforced by ``guard``:
    #   - superuser only: core settings.
    #   - operator+: fleet-wide config/provisioning (policy, webfilter mutation,
    #     token rotation, host removal).
    #   - user (host-scoped): reads and routine operations on assigned hosts.
    op = {"min_role": "operator"}
    su = {"min_role": "superuser"}
    scoped = {"host_param": "id"}
    op_scoped = {"min_role": "operator", "host_param": "id"}
    return [
        Route("/", index),
        Route("/assets/{name}", asset),
        Route("/api/about", guard(api_about)),
        Route("/api/changelog", guard(api_changelog)),
        Route("/api/policy/rules", guard(api_policy_list, **op)),
        Route("/api/policy/rules", guard(api_policy_add, **op), methods=["POST"]),
        Route(
            "/api/policy/rules/{id}",
            guard(api_policy_remove, **op),
            methods=["DELETE"],
        ),
        Route("/api/settings", guard(api_settings_list, **su)),
        Route("/api/settings/{key}", guard(api_settings_set, **su), methods=["PUT"]),
        Route(
            "/api/settings/{key}", guard(api_settings_reset, **su), methods=["DELETE"]
        ),
        Route("/api/fleet", guard(api_fleet)),
        Route("/api/fleet/overview", guard(api_fleet_overview)),
        Route("/api/fleet/trend", guard(api_fleet_trend)),
        Route("/api/digest/preview", guard(api_digest_preview, **op)),
        Route("/api/audit", guard(api_audit)),
        Route("/api/events", guard(api_events)),
        Route("/api/agent/{id}", guard(api_agent, **scoped)),
        Route("/api/agent/{id}", guard(api_remove_host, **op), methods=["DELETE"]),
        Route("/api/agent/{id}/changes", guard(api_agent_changes, **scoped)),
        Route("/api/agent/{id}/trends", guard(api_agent_trends, **scoped)),
        Route("/api/agent/{id}/refresh", guard(api_refresh, **scoped), methods=["POST"]),
        Route(
            "/api/agent/{id}/remotehelp",
            guard(api_remotehelp, **scoped),
            methods=["POST"],
        ),
        Route("/api/agent/{id}/screenshot", guard(api_screenshot, **scoped)),
        Route(
            "/api/agent/{id}/screenshot", guard(api_capture, **scoped), methods=["POST"]
        ),
        Route("/api/agent/{id}/webfilter", guard(api_webfilter_get, **scoped)),
        Route(
            "/api/agent/{id}/webfilter/config",
            guard(api_webfilter_config, **op_scoped),
            methods=["PUT"],
        ),
        Route(
            "/api/agent/{id}/webfilter/domains",
            guard(api_webfilter_add_domain, **op_scoped),
            methods=["POST"],
        ),
        Route(
            "/api/agent/{id}/webfilter/domains/{domain}",
            guard(api_webfilter_remove_domain, **op_scoped),
            methods=["DELETE"],
        ),
        Route(
            "/api/agent/{id}/webfilter/apply",
            guard(api_webfilter_apply, **op_scoped),
            methods=["POST"],
        ),
        Route("/api/agent/{id}/webfilter/activity", guard(api_webfilter_activity, **scoped)),
        Route("/api/agents/{id}/token", guard(api_rotate_token, **op), methods=["POST"]),
    ]


def _anthropic_client() -> Any:
    """Construct the real Anthropic client (lazy import; needs ANTHROPIC_API_KEY)."""

    import anthropic

    return anthropic.Anthropic()


def _sse(event: dict[str, Any]) -> bytes:
    """Encode one chat event as a Server-Sent Events ``data:`` frame."""

    return f"data: {json.dumps(event, default=str)}\n\n".encode()


def _chat_model(request: Request) -> str | None:
    """Resolve the live chat model from settings (DB > env > default).

    Returns ``None`` when settings are unavailable so ``chat.py`` falls back to
    its own env/default resolution.
    """

    settings = getattr(request.app.state, "settings", None)
    return settings.get("KENNY_CHAT_MODEL") if settings is not None else None


def build_chat_routes(
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
    sessions: ChatSessions,
    screenshots: ScreenshotStore,
    history_store: ChatHistoryStore,
    client_factory: Any = _anthropic_client,
) -> list[Route]:
    """Build the server-hosted Claude chat routes.

    * ``POST /api/chat`` — send a user message; returns a structured turn result
      (assistant text, tool events, and any pending state-changing call).
    * ``POST /api/chat/confirm`` — approve/deny a pending state-changing call,
      then resume the turn.
    * ``GET /api/chat/history`` — list persisted conversations (summary only).
    * ``GET /api/chat/history/{id}`` — one conversation's full replayable
      transcript (ADR-0027).
    * ``DELETE /api/chat/history/{id}`` — delete a persisted conversation.

    All inherit operator auth from ``OperatorAuthMiddleware`` (``/api/*``).
    ``client_factory`` is injected so tests pass a fake Anthropic client.
    """

    executor = ChatExecutor(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        screenshots=screenshots,
    )

    async def api_chat(request: Request) -> JSONResponse:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)
        session = sessions.get_or_create(body.get("session_id"))
        if session.pending is not None:
            return JSONResponse(
                {
                    "error": "a confirmation is pending; resolve it first",
                    "pending": session.pending.to_public(),
                    "session_id": session.id,
                },
                status_code=409,
            )
        # Context-aware chat: remember the dashboard's selected agent on the
        # session so forwarded capability tools target that machine (ADR-0042)
        # and the model is told about it too (see chat._context_note). This is
        # session-local state, not a shared registry slot — concurrent chat
        # sessions never clobber each other's selection. Always sync, including
        # clearing back to None when the dashboard switches to fleet-wide —
        # otherwise the session would keep pointing (and telling the model) at
        # a stale agent.
        agent_id = str(body.get("agent_id", "")).strip()
        session.agent_id = agent_id or None
        try:
            result = await run_turn(
                session, message, executor=executor, client=client_factory(),
                model=_chat_model(request),
            )
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            return JSONResponse({"error": str(exc), "session_id": session.id}, status_code=502)
        await persist_session(history_store, session)
        return JSONResponse(result.to_public())

    async def api_chat_confirm(request: Request) -> JSONResponse:
        body = await request.json()
        session_id = body.get("session_id")
        session = await sessions.get(session_id) if session_id else None
        if session is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if session.pending is None:
            return JSONResponse({"error": "no pending confirmation"}, status_code=409)
        approve = bool(body.get("approve", False))
        try:
            result = await confirm_pending(
                session, approve=approve, executor=executor, client=client_factory(),
                model=_chat_model(request),
            )
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            return JSONResponse({"error": str(exc), "session_id": session.id}, status_code=502)
        await persist_session(history_store, session)
        return JSONResponse(result.to_public())

    # SSE response headers: disable proxy/browser buffering so tokens flush live.
    _STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    async def api_chat_stream(request: Request) -> Response:
        """Streaming twin of ``/api/chat``: emit chat events as Server-Sent Events.

        Pre-stream validation (empty message, pending-409) returns JSON *before*
        the first byte; once the stream starts the status is fixed at 200, so any
        later failure is surfaced in-band as an ``error`` event.
        """

        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)
        session = sessions.get_or_create(body.get("session_id"))
        if session.pending is not None:
            return JSONResponse(
                {
                    "error": "a confirmation is pending; resolve it first",
                    "pending": session.pending.to_public(),
                    "session_id": session.id,
                },
                status_code=409,
            )
        # See api_chat above: always sync session.agent_id (including clearing
        # it back to None) so it never lags the dashboard's current selection.
        agent_id = str(body.get("agent_id", "")).strip()
        session.agent_id = agent_id or None
        client = client_factory()
        model = _chat_model(request)

        async def gen() -> Any:
            try:
                async for ev in run_turn_events(
                    session, message, executor=executor, client=client, model=model
                ):
                    yield _sse(ev)
            except Exception as exc:  # noqa: BLE001 - surface to the UI in-band
                yield _sse({"type": "error", "error": str(exc), "session_id": session.id})
                return
            # Persist only after the loop fully drains — never per-event — so an
            # aborted stream (operator Stop) leaves nothing inconsistent saved;
            # the next turn's heal_session() cleans up as it does today.
            await persist_session(history_store, session)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=_STREAM_HEADERS)

    async def api_chat_confirm_stream(request: Request) -> Response:
        """Streaming twin of ``/api/chat/confirm``."""

        body = await request.json()
        session_id = body.get("session_id")
        session = await sessions.get(session_id) if session_id else None
        if session is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if session.pending is None:
            return JSONResponse({"error": "no pending confirmation"}, status_code=409)
        approve = bool(body.get("approve", False))
        client = client_factory()
        model = _chat_model(request)

        async def gen() -> Any:
            try:
                async for ev in confirm_pending_events(
                    session, approve=approve, executor=executor, client=client, model=model
                ):
                    yield _sse(ev)
            except Exception as exc:  # noqa: BLE001 - surface to the UI in-band
                yield _sse({"type": "error", "error": str(exc), "session_id": session.id})
                return
            await persist_session(history_store, session)

        return StreamingResponse(gen(), media_type="text/event-stream", headers=_STREAM_HEADERS)

    async def api_chat_history_list(request: Request) -> JSONResponse:
        """List persisted conversations, newest-updated first (no message bodies)."""

        rows = await history_store.list()
        return JSONResponse({"conversations": rows})

    async def api_chat_history_get(request: Request) -> JSONResponse:
        """One conversation's full replayable transcript (ADR-0027)."""

        row = await history_store.get(request.path_params["id"])
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "id": row["id"],
                "title": row["title"],
                "agent_id": row["agent_id"],
                "updated_at": row["updated_at"],
                "transcript": public_transcript(row["messages"]),
            }
        )

    async def api_chat_history_delete(request: Request) -> JSONResponse:
        """Delete a persisted conversation (operator-triggered, manual only)."""

        conversation_id = request.path_params["id"]
        removed = await history_store.delete(conversation_id)
        sessions.forget(conversation_id)
        if not removed:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def api_recommendation_stream(request: Request) -> Response:
        """Stream a Haiku "AI Recommendation" for one flagged section as SSE.

        Body: ``{agent_id, section}``. Pre-stream validation returns JSON
        (``400`` missing/unknown/healthy section, ``503`` if no API key); once
        streaming starts the status is fixed at 200 and failures surface in-band
        as an ``error`` event. Inherits operator auth from the ``/api`` middleware.
        """

        body = await request.json()
        agent_id = str(body.get("agent_id", "")).strip()
        section = str(body.get("section", "")).strip()
        if not agent_id or not section:
            return JSONResponse({"error": "agent_id and section are required"}, status_code=400)
        if not ai_available():
            return JSONResponse(
                {"error": "AI recommendations are not configured"}, status_code=503
            )
        latest = await store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        facts = warning_facts(snapshot, section)
        if facts is None:
            return JSONResponse(
                {"error": "section is not flagged or has no telemetry"}, status_code=400
            )
        client = client_factory()

        async def gen() -> Any:
            try:
                async for ev in recommend_events(client, facts):
                    yield _sse(ev)
            except Exception as exc:  # noqa: BLE001 - surface to the UI in-band
                yield _sse({"type": "error", "error": str(exc)})

        return StreamingResponse(gen(), media_type="text/event-stream", headers=_STREAM_HEADERS)

    async def api_forecast_stream(request: Request) -> Response:
        """Stream one agent's near-term "AI Forecast" as SSE (ADR-0034).

        Body: ``{agent_id}``. Synthesizes the disk/battery trends and the
        inventory diff into a short prose outlook. Unlike the recommendation
        route this *always* streams 200: with no API key it streams a
        deterministic prose summary of the same facts, so the panel is never
        empty. Inherits operator auth from the ``/api`` middleware.
        """

        from datetime import datetime, timedelta, timezone

        from .. import diffs, trends

        body = await request.json()
        agent_id = str(body.get("agent_id", "")).strip()
        if not agent_id:
            return JSONResponse({"error": "agent_id is required"}, status_code=400)

        def stream_text(text: str) -> Response:
            async def gen_text() -> Any:
                yield _sse({"type": "text_delta", "text": text})
                yield _sse({"type": "done"})

            return StreamingResponse(
                gen_text(), media_type="text/event-stream", headers=_STREAM_HEADERS
            )

        latest = await store.latest(agent_id)
        if latest is None:
            return stream_text("No telemetry yet for this machine.")

        snapshot = latest["snapshot"]
        # 30-day daily history powers the disk/battery forecast; a ~1-day
        # baseline powers the inventory diff — the same windows as the /trends
        # and /changes endpoints this supersedes in the drill-down.
        now = datetime.now(timezone.utc)
        daily_30d = await store.daily_latest(
            agent_id, (now - timedelta(days=30)).date().isoformat()
        )
        daily_1d = await store.daily_latest(agent_id, (now - timedelta(days=1)).isoformat())
        baseline = daily_1d[0] if daily_1d else None
        changes = diffs.diff_snapshots(baseline["snapshot"], snapshot) if baseline else []
        agent = registry.get(agent_id)
        facts = build_facts(
            snapshot,
            trends.disk_forecast(daily_30d),
            trends.battery_trend(daily_30d),
            changes,
            agent_os=agent.os if agent else "windows",
        )

        if not ai_available():
            return stream_text(deterministic_summary(facts))

        client = client_factory()

        async def gen() -> Any:
            try:
                async for ev in forecast_events(client, facts):
                    yield _sse(ev)
            except Exception as exc:  # noqa: BLE001 - surface to the UI in-band
                yield _sse({"type": "error", "error": str(exc)})

        return StreamingResponse(gen(), media_type="text/event-stream", headers=_STREAM_HEADERS)

    return [
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/chat/confirm", api_chat_confirm, methods=["POST"]),
        Route("/api/chat/stream", api_chat_stream, methods=["POST"]),
        Route("/api/chat/confirm/stream", api_chat_confirm_stream, methods=["POST"]),
        Route("/api/chat/history", api_chat_history_list, methods=["GET"]),
        Route("/api/chat/history/{id}", api_chat_history_get, methods=["GET"]),
        Route("/api/chat/history/{id}", api_chat_history_delete, methods=["DELETE"]),
        Route("/api/recommendation/stream", api_recommendation_stream, methods=["POST"]),
        Route("/api/forecast/stream", api_forecast_stream, methods=["POST"]),
    ]


async def _known_ids(registry: AgentRegistry, store: TelemetryStore) -> list[str]:
    ids = {a.agent_id for a in registry.list()}
    ids.update(await store.known_agents())
    return sorted(ids)


async def _overview(
    agent_id: str, registry: AgentRegistry, store: TelemetryStore
) -> dict[str, Any]:
    agent = registry.get(agent_id)
    latest = await store.latest(agent_id)
    snapshot = latest["snapshot"] if latest else None
    health = build_health(snapshot, agent_os=agent.os if agent else "windows")
    sections = health["sections"]
    flagged = [n for n, s in sections.items() if s["status"] in ("warn", "crit")]

    def _by_status(level: str) -> list[dict[str, Any]]:
        # Enough detail for the dashboard to render the flagged section cards.
        return [
            {"name": n, "summary": s.get("summary", ""), "reason": s.get("reason")}
            for n, s in sections.items()
            if s["status"] == level
        ]

    return {
        "agent_id": agent_id,
        "online": bool(agent and agent.online),
        "os": agent.os if agent else "windows",
        "meta": agent.meta if agent else {},
        "overall": health["overall"],
        "flagged_sections": flagged,
        "warn_sections": _by_status("warn"),
        "crit_sections": _by_status("crit"),
        "summary": _fleet_summary(health, snapshot),
        "collected_at": latest["collected_at"] if latest else None,
    }


def _fleet_summary(health: dict[str, Any], snapshot: dict[str, Any] | None) -> str:
    """A short one-line summary for the fleet list: the worst flagged section, else 'all green'."""

    if not snapshot:
        return "no telemetry yet"
    sections = health.get("sections", {})
    for want in ("crit", "warn"):
        worst = [(n, s) for n, s in sections.items() if s["status"] == want]
        if worst:
            name, s = worst[0]
            text = s.get("reason") or s.get("summary") or name
            extra = f" +{len(worst) - 1} more" if len(worst) > 1 else ""
            return f"{text}{extra}"
    return "all green"
