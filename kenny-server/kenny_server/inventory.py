"""Remove a host from inventory (ADR-0033).

``purge_agent`` fans out across every store that keys data by ``agent_id`` plus
the in-memory registry and screenshot cache, so a removed host leaves no trace
and cannot immediately re-push. Each store owns its own connection (no shared
transaction), so deletes are best-effort in a fixed order and partial failures
are logged and reported rather than raised — mirroring the lifespan close()
fan-out in ``main.py``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("kenny.inventory")


def seeded_in_env(agent_id: str) -> bool:
    """Whether ``agent_id`` is pinned in ``KENNY_AGENT_TOKENS``.

    Such a host would be re-seeded on the next store connect, so removing it from
    inventory without first editing the env var is futile — the caller should
    refuse and say so.
    """

    raw = os.environ.get("KENNY_AGENT_TOKENS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair and pair.split("=", 1)[0].strip() == agent_id:
            return True
    return False


async def purge_agent(
    agent_id: str,
    *,
    registry,
    store,
    event_store,
    alert_state,
    token_store,
    key_store,
    webfilter_store,
    user_store,
    screenshots,
    suppression=None,
    ticket_rules=None,
) -> dict[str, str]:
    """Delete every trace of ``agent_id``; return a per-store outcome map."""

    results: dict[str, str] = {}

    async def _try(name: str, coro) -> None:
        try:
            await coro
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - best-effort, report don't raise
            logger.warning("purge %s: %s failed: %s", agent_id, name, exc)
            results[name] = f"error: {exc}"

    await _try("snapshots", store.delete_agent(agent_id))
    await _try("events", event_store.delete_agent(agent_id))
    await _try("alert_state", alert_state.delete_agent(agent_id))
    await _try("agent_token", token_store.delete(agent_id))
    await _try("agent_key", key_store.delete(agent_id))
    await _try("webfilter", webfilter_store.delete_agent(agent_id))
    await _try("user_hosts", user_store.purge_host(agent_id))
    if suppression is not None:
        # Only this host's own suppression rules go -- fleet-wide rules mute a
        # Windows quirk, not a specific PC, and must survive its removal.
        await _try("reliability_suppressions", suppression.delete_agent(agent_id))
    if ticket_rules is not None:
        # Same asymmetry as suppression rules: only this host's own auto-ticket
        # rules go, fleet-wide policy survives its removal.
        await _try("ticket_rules", ticket_rules.delete_agent(agent_id))
    # In-memory teardown: dropping the agent also nulls its send_fn reference, so
    # a still-connected socket can't be forwarded to; its token/key are gone so it
    # cannot re-authenticate.
    existed = registry.remove(agent_id)
    screenshots.forget(agent_id)
    results["registry"] = "removed" if existed else "absent"
    logger.info("purged host %s from inventory: %s", agent_id, results)
    return results
