"""Scheduled update detection + pinned, operator-approved rollout (ADR-0044).

Two independent halves, orchestrated by :class:`UpdateManager`:

* **Detection** (:meth:`check_now`, run on a timer by :func:`update_check_loop`
  from the app lifespan, exactly like the existing backup/alert loops):
  refreshes the agent-release cache (``agent_release.py``, GitHub Releases,
  unchanged) and polls GHCR (``server_release.py``, read-only) for a newer
  server image. Detection only *records* what's available — it never applies
  anything.
* **Rollout** (:meth:`approve_campaign` / :meth:`apply_now` /
  :meth:`on_agent_connect`): an operator approves an agent-update campaign,
  which **snapshots one exact artifact** (version + per-arch binary + sha256,
  copied to a durable per-campaign directory) at approval time. Every trigger
  under that campaign — a one-shot "apply now" or an on-connect auto-apply —
  sends that pinned snapshot, never whatever the shared release cache
  currently holds. This is the load-bearing distinction from a "track latest"
  toggle: a release the detection loop finds *after* approval is a separate,
  separately-approvable candidate, not something this campaign will ever ship.
  A per-agent attempt budget (``store.ATTEMPT_BUDGET``) stops a kill-switch-off
  or crash-looping agent from being retried forever.

Server-image rollout is **detect-only** in this iteration: a container cannot
replace its own running image, and a docker-socket-holding auto-apply sidecar
is a deferred, additive follow-up (ADR-0044) — not built here. The dashboard
shows the digest-pinned ``docker compose`` command; the operator runs it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__, agent_release, server_release
from .agent_release import _sha256_file
from .config import Settings
from .distribution import ShareLinks, agent_binary_path, perform_agent_update
from .registry import AgentRegistry
from .store import UpdateStore
from .tunnel import AgentTunnel, ToolError

logger = logging.getLogger("kenny.update")

# Delay after an agent connects before the on-connect campaign hook checks it,
# so it runs past the handshake/first telemetry settling rather than racing it.
ON_CONNECT_DELAY_S = 3.0


def default_campaign_dir(db_path: str) -> str:
    """Where pinned per-campaign agent binaries live, a sibling of the DB file."""

    return os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "update_campaigns")


class UpdateManager:
    """Owns detection + campaign rollout. One instance, wired in ``main.py``."""

    def __init__(
        self,
        *,
        db_path: str,
        store: UpdateStore,
        registry: AgentRegistry,
        tunnel: AgentTunnel,
        share_links: ShareLinks,
        settings: Settings,
        campaign_dir: str | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.tunnel = tunnel
        self.share_links = share_links
        self.settings = settings
        self.campaign_dir = campaign_dir or default_campaign_dir(db_path)

    # -- detection -----------------------------------------------------------

    async def check_now(self) -> dict[str, Any]:
        """One detection pass. Best-effort: never raises, never applies anything."""

        await self._expire_stale_campaign()

        agent_fetch = None
        if agent_release.github_configured():
            agent_fetch = await asyncio.to_thread(agent_release.fetch_latest_agent_binary)
        status = agent_release.binary_status(manual_path=agent_binary_path())
        await self.store.set_availability(
            "agent",
            version=status.version or "",
            sha256=status.sha256,
            ok=status.ok,
            message=(agent_fetch.message if agent_fetch is not None else status.message),
        )

        image_ref = self.settings.get("KENNY_SERVER_IMAGE_REF")
        server_result = await server_release.fetch_latest_server_tag(
            image_ref, github_token=agent_release.github_token()
        )
        if server_result.ok and server_result.tag:
            if server_release.is_newer(server_result.tag, __version__):
                await self.store.set_availability(
                    "server",
                    version=server_result.tag,
                    digest=server_result.digest,
                    ok=True,
                    message=server_result.message,
                )
            else:
                await self.store.set_availability(
                    "server", version=__version__, ok=True, message="up to date"
                )
        else:
            logger.info("server image check skipped: %s", server_result.message)

        return {"agent": status.to_public(), "server": server_result.to_public()}

    # -- campaign lifecycle ----------------------------------------------------

    async def approve_campaign(
        self,
        *,
        version: str | None = None,
        on_connect: bool = False,
        max_age_secs: int | None = None,
    ) -> dict[str, Any]:
        """Pin an agent-update campaign to one exact, already-cached version.

        Copies every (os, arch) binary currently cached at ``version`` into a
        durable per-campaign directory, so a later detection pass overwriting
        the shared release cache can never change what this campaign pushes.
        Supersedes (and cleans up) any prior active campaign. Raises
        :class:`ValueError` if no cached binary matches ``version``.
        """

        if version is None:
            avail = await self.store.get_availability("agent")
            if avail is None or not avail.get("version"):
                raise ValueError("no known agent version to approve; run a check first")
            version = avail["version"]

        prior = await self.store.get_active_campaign()

        campaign_id = uuid.uuid4().hex
        dest_dir = os.path.join(self.campaign_dir, campaign_id)
        targets: list[dict[str, str]] = []
        for os_name, arch in agent_release.SUPPORTED_TARGETS:
            cache = agent_release.cache_path(os_name, arch)
            if not os.path.exists(cache):
                continue
            if agent_release.resolve_agent_version(cache) != version:
                continue
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, f"kenny-agent-{os_name}-{arch}")
            await asyncio.to_thread(shutil.copy2, cache, dest)
            targets.append({"os": os_name, "arch": arch, "path": dest, "sha256": _sha256_file(dest)})

        if not targets:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise ValueError(f"no cached agent binary at version {version!r} to pin")

        if max_age_secs is None:
            max_age_secs = int(self.settings.get("KENNY_UPDATE_CAMPAIGN_MAX_AGE_SECS"))
        expires_at = None
        if max_age_secs and max_age_secs > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max_age_secs)).isoformat()

        await self.store.create_campaign(
            id=campaign_id,
            version=version,
            on_connect=on_connect,
            expires_at=expires_at,
            targets=targets,
        )
        if prior is not None:
            self._cleanup_campaign_dir(prior["id"])
        return await self.store.get_campaign(campaign_id)  # type: ignore[return-value]

    async def revoke_campaign(self, campaign_id: str) -> bool:
        """Stop future triggers under ``campaign_id``. Cannot recall an in-flight one."""

        ok = await self.store.set_campaign_status(campaign_id, "revoked")
        if ok:
            self._cleanup_campaign_dir(campaign_id)
        return ok

    async def apply_now(self, campaign_id: str | None = None) -> dict[str, Any]:
        """Apply a pinned campaign to every currently-online, eligible agent."""

        campaign = (
            await self.store.get_campaign(campaign_id)
            if campaign_id
            else await self.store.get_active_campaign()
        )
        if campaign is None:
            raise ValueError("no active campaign")
        if campaign["status"] != "active":
            raise ValueError(f"campaign is {campaign['status']}, not active")
        attempted = []
        for agent in self.registry.list():
            if not agent.online:
                continue
            await self._apply_to_agent(campaign, agent)
            attempted.append(agent.agent_id)
        return {"campaign_id": campaign["id"], "attempted": attempted}

    async def on_agent_connect(self, agent_id: str) -> None:
        """Tunnel on-connect hook: auto-apply the active campaign, if enabled.

        Fire-and-forget from the tunnel (never awaited by the handshake path) —
        any failure here must never affect the agent connection, so every
        exception is swallowed and logged.
        """

        try:
            # Two gates, both must be open: the global setting (an operator-wide
            # kill switch for the whole on-connect behavior) and the specific
            # campaign's own on_connect flag (set at approval time) — a campaign
            # approved as "apply now only" must not start auto-applying just
            # because the global setting is later flipped on.
            if not self.settings.get("KENNY_AGENT_ROLLOUT_ON_CONNECT"):
                return
            campaign = await self.store.get_active_campaign()
            if campaign is None or not campaign["on_connect"]:
                return
            await asyncio.sleep(ON_CONNECT_DELAY_S)
            agent = self.registry.get(agent_id)
            if agent is None or not agent.online:
                return
            await self._apply_to_agent(campaign, agent)
        except Exception:  # noqa: BLE001 - a hook failure must never affect the tunnel
            logger.exception("on-connect update campaign apply failed for %s", agent_id)

    # -- read model for the dashboard ------------------------------------------

    async def fleet_status(self) -> dict[str, Any]:
        availability = await self.store.list_availability()
        campaign = await self.store.get_active_campaign()
        campaigns = await self.store.list_campaigns()
        agents_out: list[dict[str, Any]] = []
        if campaign is not None:
            targets = await self.store.campaign_targets(campaign["id"])
            states = await self.store.list_agent_states(campaign["id"])
            for agent in self.registry.list():
                eligible = any(t["os"] == agent.os and t["arch"] == agent.arch for t in targets)
                state = states.get(agent.agent_id, {})
                agents_out.append(
                    {
                        "agent_id": agent.agent_id,
                        "online": agent.online,
                        "os": agent.os,
                        "arch": agent.arch,
                        "current_version": (agent.meta or {}).get("version"),
                        "eligible": eligible,
                        "attempts": state.get("attempts", 0),
                        "held": state.get("held", False),
                        "updated": state.get("updated_version", False),
                    }
                )
        return {
            "available": availability,
            "active_campaign": campaign,
            "campaigns": campaigns,
            "agents": agents_out,
        }

    # -- internals ---------------------------------------------------------

    async def _apply_to_agent(self, campaign: dict[str, Any], agent: Any) -> None:
        targets = await self.store.campaign_targets(campaign["id"])
        target = next((t for t in targets if t["os"] == agent.os and t["arch"] == agent.arch), None)
        if target is None:
            # No pinned artifact for this agent's (os, arch) under this
            # campaign — a server-side gap (the release didn't cover this
            # target), not an agent failure. Never counted against the budget.
            return

        current_version = str((agent.meta or {}).get("version") or "")
        if current_version == campaign["version"]:
            await self.store.record_attempt(campaign["id"], agent.agent_id, ok=True)
            await self._maybe_complete(campaign)
            return

        state = await self.store.get_agent_state(campaign["id"], agent.agent_id)
        if state is not None and (state["held"] or state["updated_version"]):
            return

        try:
            await perform_agent_update(
                self.tunnel,
                self.share_links,
                agent.agent_id,
                os_name=target["os"],
                arch=target["arch"],
                version=campaign["version"],
                binary_path=target["path"],
                sha256=target["sha256"],
            )
            await self.store.record_attempt(campaign["id"], agent.agent_id, ok=True)
        except ToolError as exc:
            # An anti-cheat "paused" refusal (ADR-0039) is expected to clear on
            # its own — retry later without spending the attempt budget.
            # disabled (kill-switch, ADR-0011) and blocked (deny-guard) count.
            await self.store.record_attempt(
                campaign["id"],
                agent.agent_id,
                ok=False,
                error=f"{exc.code}: {exc.message}",
                count_against_budget=exc.code != "paused",
            )
        except Exception as exc:  # noqa: BLE001 - one bad agent must not break the rollout
            await self.store.record_attempt(
                campaign["id"], agent.agent_id, ok=False, error=str(exc)
            )
        await self._maybe_complete(campaign)

    async def _maybe_complete(self, campaign: dict[str, Any]) -> None:
        if campaign["status"] != "active":
            return
        targets = await self.store.campaign_targets(campaign["id"])
        eligible = [
            a
            for a in self.registry.list()
            if any(t["os"] == a.os and t["arch"] == a.arch for t in targets)
        ]
        if not eligible:
            return
        # Deliberately does NOT treat a "held" agent as done: a held agent has
        # not reached the target version, only stopped being auto-retried, so
        # the campaign must stay active (and visible to the operator) rather
        # than being marked "completed" — a status that would misleadingly
        # read as full success. Expiry (KENNY_UPDATE_CAMPAIGN_MAX_AGE_SECS) is
        # what eventually closes out a campaign stuck on a held agent.
        states = await self.store.list_agent_states(campaign["id"])
        done = all(
            (states.get(a.agent_id) or {}).get("updated_version")
            or (a.meta or {}).get("version") == campaign["version"]
            for a in eligible
        )
        if done:
            await self.store.set_campaign_status(campaign["id"], "completed")
            self._cleanup_campaign_dir(campaign["id"])

    async def _expire_stale_campaign(self) -> None:
        campaign = await self.store.get_active_campaign()
        if campaign is None or not campaign.get("expires_at"):
            return
        try:
            expires = datetime.fromisoformat(campaign["expires_at"])
        except ValueError:
            return
        if datetime.now(timezone.utc) >= expires:
            await self.store.set_campaign_status(campaign["id"], "expired")
            self._cleanup_campaign_dir(campaign["id"])

    def _cleanup_campaign_dir(self, campaign_id: str) -> None:
        shutil.rmtree(os.path.join(self.campaign_dir, campaign_id), ignore_errors=True)


async def update_check_loop(
    update_mgr: UpdateManager, settings: Settings, interval_s: int, initial_delay_s: float
) -> None:
    """Periodically run one detection pass (best-effort, mirrors ``_backup_loop``)."""

    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await update_mgr.check_now()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("scheduled update check failed")
        # Re-read the cadence each pass so a dashboard change retimes the loop.
        interval = settings.get("KENNY_UPDATE_CHECK_INTERVAL_SECS")
        await asyncio.sleep(interval if interval and interval > 0 else interval_s)
