"""Scheduled update detection + pinned, operator-approved rollout (ADR-0040).

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
is a deferred, additive follow-up (ADR-0040) — not built here. The dashboard
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
from .store import UpdateStore, _availability_key
from .tunnel import AgentTunnel, ToolError

logger = logging.getLogger("kenny.update")

# Delay after an agent connects before the on-connect campaign hook checks it,
# so it runs past the handshake/first telemetry settling rather than racing it.
ON_CONNECT_DELAY_S = 3.0


async def record_agent_fetch(
    store: UpdateStore, result: agent_release.FetchResult, *, channel: str = "stable"
) -> None:
    """Persist one agent-binary fetch outcome to the durable availability row.

    Three code paths attempt (or deliberately skip) that fetch — startup
    (``main.py``), the detection loop below, and the operator's manual retry
    (``distribution.agent_binary_fetch``) — and each used to record it somewhere
    different, or not at all. ADR-0040 already made ``update_availability`` the
    durable record of detection outcomes; this is the one door into it, so a
    failure survives the restart that ``app.state.last_fetch`` does not.

    Best-effort by contract: a store hiccup must never break startup or turn a
    status read into a 500.
    """

    try:
        # `version`/`sha256` identify the artifact that is now staged — read off
        # disk, so they stay right when the fetch failed and the previous binary
        # is still what a new PC would receive. `ok`/`message` describe the
        # attempt. Both meanings, kept apart.
        staged = agent_release.binary_status(
            manual_path=agent_binary_path(channel=channel), channel=channel
        )
        await store.set_availability(
            _availability_key("agent", channel),
            version=staged.version or "",
            sha256=staged.sha256,
            ok=result.ok,
            message=result.message,
        )
    except Exception as exc:  # noqa: BLE001 - recording an outcome cannot become an outage
        logger.warning("could not record agent fetch outcome: %s", exc)


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

        # The row records the **fetch**, in both fields. It used to carry
        # `ok` from the on-disk probe and `message` from the fetch, so a failed
        # refresh could read as ok=True next to its own error text. Whether a
        # binary is present is already in `available`/`targets`.
        agent_fetch = await asyncio.to_thread(agent_release.fetch_latest_agent_binary)
        await record_agent_fetch(self.store, agent_fetch)
        status = agent_release.binary_status(manual_path=agent_binary_path())

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
            # An unreachable image ref is a misconfiguration the operator should
            # see, not routine noise. GHCR reads anonymously for a public
            # package, so there is no "not configured" case left to excuse it.
            logger.warning("server image check failed: %s", server_result.message)

        status_dev = await self._check_agent_dev()
        server_result_dev = await self._check_server_dev(image_ref)

        return {
            "agent": status.to_public(),
            "server": server_result.to_public(),
            "agent_dev": status_dev.to_public(),
            "server_dev": server_result_dev.to_public(),
        }

    async def _check_agent_dev(self) -> agent_release.FetchResult:
        """Dev-channel counterpart of the agent-binary check above.

        Independently best-effort: any failure here (fetch or cache-probe) must
        never affect the stable ``agent`` availability row recorded above.
        """

        try:
            agent_fetch_dev = await asyncio.to_thread(
                agent_release.fetch_latest_agent_binary, channel="dev"
            )
            await record_agent_fetch(self.store, agent_fetch_dev, channel="dev")
            status_dev = agent_release.binary_status(
                manual_path=agent_binary_path(channel="dev"), channel="dev"
            )
            return status_dev
        except Exception as exc:  # noqa: BLE001 - a dev-poll failure must never affect the stable result
            logger.info("agent dev-channel check skipped: %s", exc)
            return agent_release.FetchResult(ok=False, source="none", message=f"dev check failed: {exc}")

    async def _check_server_dev(self, image_ref: str) -> server_release.ServerReleaseInfo:
        """Dev-channel counterpart of the server-image check above.

        The "current" side of the newer-than comparison reuses ``__version__``:
        a dev-channel server build's ``KENNY_SERVER_VERSION`` is itself a
        ``X.Y.Z-dev.N`` string once CI stamps it. If ``__version__`` doesn't
        parse as a dev-prerelease (e.g. this server is running stable), that
        means "no dev baseline" — the dev candidate is always reported as
        available rather than hidden, mirroring how the stable path already
        treats an unparseable current version as "not comparable", but erring
        toward showing the dev availability since an operator on the stable
        server channel still wants to see "here's the latest dev build" for
        provisioning a fresh dev test PC.
        """

        try:
            server_result_dev = await server_release.fetch_latest_server_tag(
                image_ref, github_token=agent_release.github_token(), channel="dev"
            )
            if server_result_dev.ok and server_result_dev.tag:
                show_available = (
                    server_release._parse_dev_comparable(__version__) is None
                    or server_release.is_newer(server_result_dev.tag, __version__, channel="dev")
                )
                if show_available:
                    await self.store.set_availability(
                        _availability_key("server", "dev"),
                        version=server_result_dev.tag,
                        digest=server_result_dev.digest,
                        ok=True,
                        message=server_result_dev.message,
                    )
                else:
                    await self.store.set_availability(
                        _availability_key("server", "dev"),
                        version=__version__,
                        ok=True,
                        message="up to date",
                    )
            else:
                logger.info("server dev-channel image check skipped: %s", server_result_dev.message)
            return server_result_dev
        except Exception as exc:  # noqa: BLE001 - a dev-poll failure must never affect the stable result
            logger.info("server dev-channel check skipped: %s", exc)
            return server_release.ServerReleaseInfo(ok=False, message=f"dev check failed: {exc}")

    # -- campaign lifecycle ----------------------------------------------------

    async def approve_campaign(
        self,
        *,
        version: str | None = None,
        channel: str = "stable",
        on_connect: bool = False,
        max_age_secs: int | None = None,
    ) -> dict[str, Any]:
        """Pin an agent-update campaign to one exact, already-cached version.

        Copies every (os, arch) binary currently cached at ``version`` (for
        ``channel``) into a durable per-campaign directory, so a later
        detection pass overwriting the shared release cache can never change
        what this campaign pushes. Supersedes (and cleans up) any prior active
        campaign **for the same channel** — an active stable campaign and an
        active dev campaign coexist independently (ADR-0048). Raises
        :class:`ValueError` if no cached binary matches ``version``.
        """

        if version is None:
            avail = await self.store.get_availability(_availability_key("agent", channel))
            if avail is None or not avail.get("version"):
                raise ValueError("no known agent version to approve; run a check first")
            version = avail["version"]

        prior = await self.store.get_active_campaign(channel=channel)

        campaign_id = uuid.uuid4().hex
        dest_dir = os.path.join(self.campaign_dir, campaign_id)
        targets: list[dict[str, str]] = []
        for os_name, arch in agent_release.SUPPORTED_TARGETS:
            cache = agent_release.cache_path(os_name, arch, channel)
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
            channel=channel,
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

    async def suspend_campaign(self, campaign_id: str) -> bool:
        """Pause ``campaign_id`` without discarding it: stop both triggers, keep everything.

        Unlike :meth:`revoke_campaign`, this is deliberately *not* terminal and
        does **not** clean up the pinned artifact directory — there would be
        nothing left to resume from otherwise (ADR-0040's *More Information*).
        It also leaves ``update_campaign_agents`` untouched, which is the
        entire reason this state exists rather than "revoke, then re-approve
        later": a fresh campaign gets a fresh ``campaign_id``, and per-agent
        attempt/held bookkeeping is keyed ``(campaign_id, agent_id)`` — so
        recreating would silently hand a held, likely crash-looping agent a
        brand-new attempt budget. Suspending and later resuming the *same*
        campaign keeps that agent held.

        Only an ``active`` campaign can be suspended — a revoked, expired, or
        already-completed campaign is refused (``False``), not silently
        no-opped into a new state.
        """

        return await self.store.set_campaign_status(campaign_id, "suspended")

    async def resume_campaign(self, campaign_id: str) -> bool:
        """Reactivate a suspended campaign: :meth:`apply_now` and the on-connect
        hook see it again, exactly as before it was suspended.

        Only a ``suspended`` campaign can be resumed.
        """

        return await self.store.set_campaign_status(
            campaign_id, "active", from_status="suspended"
        )

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
            # Look up the active campaign for *this agent's desired channel*
            # (ADR-0048) — an agent an operator just flipped to dev, but which
            # hasn't updated yet, still reports its old (stable) actual
            # channel, so the campaign lookup must key off desired, not actual.
            desired_channel = await self.store.get_desired_channel(agent_id)
            campaign = await self.store.get_active_campaign(channel=desired_channel)
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
        agents_out = await self._agents_for_campaign(campaign)
        # Dev-channel counterparts, additive (ADR-0048): a stable and a dev
        # campaign can be active simultaneously (store.py keys them apart by
        # channel), so the dashboard needs to see both to approve/track a dev
        # rollout without disturbing the existing stable fields above.
        campaign_dev = await self.store.get_active_campaign(channel="dev")
        campaigns_dev = await self.store.list_campaigns(channel="dev")
        agents_out_dev = await self._agents_for_campaign(campaign_dev)
        return {
            "available": availability,
            "active_campaign": campaign,
            "campaigns": campaigns,
            "agents": agents_out,
            "active_campaign_dev": campaign_dev,
            "campaigns_dev": campaigns_dev,
            "agents_dev": agents_out_dev,
        }

    async def _agents_for_campaign(self, campaign: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Per-agent eligibility/progress rows for ``campaign`` (or ``[]`` if None).

        Eligibility (ADR-0040, ADR-0048) requires both a matching (os, arch)
        target under the campaign *and* the agent's **desired** channel (soll,
        operator-set) matching the campaign's channel — not the agent's
        actual/reported channel (ist), so an agent an operator just flipped to
        dev, but which hasn't updated yet, is still eligible for the dev
        campaign that will bring it there.
        """

        if campaign is None:
            return []
        targets = await self.store.campaign_targets(campaign["id"])
        states = await self.store.list_agent_states(campaign["id"])
        out: list[dict[str, Any]] = []
        for agent in self.registry.list():
            desired_channel = await self.store.get_desired_channel(agent.agent_id)
            eligible = (
                any(t["os"] == agent.os and t["arch"] == agent.arch for t in targets)
                and desired_channel == campaign["channel"]
            )
            state = states.get(agent.agent_id, {})
            out.append(
                {
                    "agent_id": agent.agent_id,
                    "online": agent.online,
                    "os": agent.os,
                    "arch": agent.arch,
                    "channel": agent.channel,
                    "desired_channel": desired_channel,
                    "current_version": (agent.meta or {}).get("version"),
                    "eligible": eligible,
                    "attempts": state.get("attempts", 0),
                    "held": state.get("held", False),
                    "updated": state.get("updated_version", False),
                }
            )
        return out

    async def set_desired_channel(self, agent_id: str, channel: str) -> None:
        """Set the operator-desired release channel for ``agent_id`` (ADR-0048).

        Thin passthrough to ``store.UpdateStore.set_desired_channel`` so the
        web API layer doesn't reach into the store directly — this module owns
        the read/write model the dashboard's Updates tab consumes.
        """

        await self.store.set_desired_channel(agent_id, channel)

    # -- internals ---------------------------------------------------------

    async def _apply_to_agent(self, campaign: dict[str, Any], agent: Any) -> None:
        desired_channel = await self.store.get_desired_channel(agent.agent_id)
        if desired_channel != campaign.get("channel", "stable"):
            # Not eligible under this campaign's channel (ADR-0048) — same
            # no-op-without-penalty treatment as a missing (os, arch) target.
            return
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
            # An anti-cheat "paused" refusal (ADR-0035) is expected to clear on
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
        campaign_channel = campaign.get("channel", "stable")
        eligible: list[Any] = []
        for a in self.registry.list():
            if not any(t["os"] == a.os and t["arch"] == a.arch for t in targets):
                continue
            if await self.store.get_desired_channel(a.agent_id) != campaign_channel:
                continue
            eligible.append(a)
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
        # A stable and a dev campaign can be active simultaneously (ADR-0048,
        # store.py's per-channel active-campaign uniqueness) — both need their
        # own expiry check, independently.
        for channel in ("stable", "dev"):
            await self._expire_stale_campaign_for_channel(channel)

    async def _expire_stale_campaign_for_channel(self, channel: str) -> None:
        campaign = await self.store.get_active_campaign(channel=channel)
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
