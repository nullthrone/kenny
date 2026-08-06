"""Real Rust<->Python end-to-end test (no mock agent).

Runs the composed server on uvicorn and spawns the **actual** compiled
`kenny-agent` binary, which dials `/agent/ws`, registers, serves a forwarded shell
call — `shell_exec` via real `sh` on Linux, `powershell_exec` via real
`powershell.exe` on Windows (each is `unsupported` on the other OS, and the
server's OS guard refuses the wrong one before ever forwarding it, see
docs/protocol.md § "OS-scoped tools") — and pushes a telemetry
snapshot. Asserts the round-trip through the real wire protocol on both sides.

On a Windows runner the test additionally drives the real `#[cfg(windows)]` tool
paths (CIM/WMI diagnostics, `ipconfig /flushdns`, winget) that return `unsupported`
on the Linux build — see `_assert_windows_tools`. Set `KENNY_E2E_FULL=1` (intended
for a self-hosted runner with an interactive desktop) to also exercise
`screen_capture`.

On a Linux runner it instead drives the real Linux paths (ADR-0031): the portable
`diag_processes`/`net_config` tools, the boundary where a Windows-only tool
(`winget_list`) returns `unsupported`, and the `#[cfg(target_os = "linux")]`
collectors read back through the snapshot — `installed_software` from dpkg,
`local_accounts` from `/etc/passwd`, `listening_ports` from `/proc/net` — plus the
systemd-backed `services`/`time_sync` sections (structural only, since a sandbox may
have no systemd bus). See `_assert_linux`.

Skipped unless the agent binary is available: set ``KENNY_AGENT_BIN`` to its path,
or build it (``cd kenny-agent && cargo build``) so the default debug path exists.
Set ``KENNY_E2E=1`` to force-fail (instead of skip) when the binary is missing.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kenny_server.main import build_app

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BIN = REPO_ROOT / "kenny-agent" / "target" / "debug" / "kenny-agent"

# This test's own time budget, named explicitly instead of left as inline
# polling literals, so it can be summed into a per-test `pytest.mark.timeout`
# below instead of relying on the suite-wide `timeout = 90` in pyproject.toml.
# That ini bound exists to catch a genuinely *hung* test (see its comment); this
# test is not hung, it is just slow on Windows, and needs a bound sized from
# what it actually does. `tests/test_e2e_timeout_budget.py` asserts the two
# stay in agreement.
_REGISTER_POLL_INTERVAL_S = 0.1
_REGISTER_POLLS = 100  # ~10s for the real agent to dial in and register
_REGISTER_BUDGET_S = _REGISTER_POLLS * _REGISTER_POLL_INTERVAL_S

# Timeout for the very first tool call after registration (a bare "echo hi"
# smoke test, before telemetry or the OS-specific assertions). On a Windows
# release runner this is the *first* PowerShell process the freshly-built
# release binary ever spawns, which observably hit real-time antivirus
# scanning of the new .exe/child process and blew a 20s ceiling outright
# (see the "Release (dev channel)" run that failed on this exact line).
# Later PowerShell calls in `_assert_windows_tools` are not nearly this slow
# once the runner is warm -- their aggregate budget below is ~7-9s/call.
_SMOKE_CALL_TIMEOUT_S = 60 if sys.platform == "win32" else 20

_TELEMETRY_POLL_INTERVAL_S = 0.2
# Windows collectors spawn PowerShell/CIM and are far slower on a cold hosted
# runner than the Linux sysinfo collectors, so they get a much longer window.
_TELEMETRY_POLLS = 600 if sys.platform == "win32" else 50  # ~120s / ~10s
_TELEMETRY_BUDGET_S = _TELEMETRY_POLLS * _TELEMETRY_POLL_INTERVAL_S

# Allowance for the OS-specific tool-call assertions after telemetry arrives.
# Windows needs the most: diag_processes, diag_services (CIM Win32_Service),
# diag_eventlog (Get-WinEvent), net_config, net_dns_flush (real
# `ipconfig /flushdns`), the shell_exec OS-guard boundary, winget_list, and
# (under KENNY_E2E_FULL=1) screen_capture. Observed release-runner wall time
# for this section alone was ~55-70s; this leaves real headroom above that.
# The Linux side finishes in low single-digit seconds in practice, but is
# still given enough headroom that its own summed budget clears the 90s
# suite-wide ini timeout (see test_e2e_timeout_budget.py) -- a per-test mark
# that doesn't actually exceed the ini bound would be dead weight.
_TOOLCALL_BUDGET_S = 180 if sys.platform == "win32" else 90

_TEST_TIMEOUT_S = (
    _REGISTER_BUDGET_S + _SMOKE_CALL_TIMEOUT_S + _TELEMETRY_BUDGET_S + _TOOLCALL_BUDGET_S
)


def _agent_bin() -> Path | None:
    env = os.environ.get("KENNY_AGENT_BIN")
    if env:
        candidate = Path(env)
    else:
        # `cargo build` emits `kenny-agent.exe` on Windows.
        candidate = _DEFAULT_BIN.with_suffix(".exe") if sys.platform == "win32" else _DEFAULT_BIN
    return candidate if candidate.exists() else None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_Server":
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.should_exit = True
        if self._task is not None:
            await self._task


@pytest.mark.asyncio
@pytest.mark.timeout(_TEST_TIMEOUT_S)
async def test_real_agent_end_to_end(tmp_path) -> None:
    binary = _agent_bin()
    if binary is None:
        msg = f"agent binary not found (looked at {_DEFAULT_BIN}); build with `cargo build`"
        if os.environ.get("KENNY_E2E") == "1":
            pytest.fail(msg)
        pytest.skip(msg)

    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e.sqlite"))
    token = app.state.operator_token

    async with _Server(app, port):
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--server",
            f"ws://127.0.0.1:{port}/agent/ws",
            "--agent-id",
            "dev",
            "--token",
            "dev-token",
            "--telemetry-interval-secs",
            "2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "RUST_LOG": "warn"},
        )
        try:
            # Wait for the real agent to dial in and register.
            registry = app.state.registry
            for _ in range(_REGISTER_POLLS):
                agent = registry.get("dev")
                if agent is not None and agent.online:
                    break
                await asyncio.sleep(_REGISTER_POLL_INTERVAL_S)
            else:
                raise AssertionError(f"real agent did not register within {_REGISTER_BUDGET_S:.0f}s")

            transport = StreamableHttpTransport(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": f"Bearer {token}"},
            )
            async with Client(transport) as client:
                # select_agent is advisory only (ADR-0038); forwarded capability
                # calls require their own agent_id naming the target host.
                await client.call_tool("select_agent", {"id": "dev"})
                if sys.platform == "win32":
                    res = await client.call_tool(
                        "powershell_exec",
                        {"args": {"script": "echo hi", "timeout_s": _SMOKE_CALL_TIMEOUT_S,
                                  "agent_id": "dev"}},
                    )
                else:
                    # shell_exec is powershell_exec's OS-scoped mirror; the real
                    # agent runs it via `sh -c "echo hi"` on Linux.
                    res = await client.call_tool(
                        "shell_exec",
                        {"args": {"command": "echo hi", "timeout_s": _SMOKE_CALL_TIMEOUT_S,
                                  "agent_id": "dev"}},
                    )
                assert res.data["exit_code"] == 0
                assert "hi" in res.data["stdout"]

                # The agent pushes telemetry on its first tick; wait for it. Windows
                # collectors spawn PowerShell/CIM and are far slower on a cold runner
                # than the Linux sysinfo collectors, so allow a much longer window.
                for _ in range(_TELEMETRY_POLLS):
                    fleet = (await client.call_tool("fleet_overview", {})).data
                    dev = next(
                        (a for a in fleet["agents"] if a["agent_id"] == "dev"), None
                    )
                    if dev is not None and dev["collected_at"]:
                        break
                    await asyncio.sleep(_TELEMETRY_POLL_INTERVAL_S)
                else:
                    raise AssertionError(
                        f"no telemetry snapshot arrived from the agent within {_TELEMETRY_BUDGET_S:.0f}s"
                    )

                assert dev["online"] is True
                # Real collectors produce a valid overall health (any level).
                assert dev["overall"] in ("ok", "warn", "crit")

                # On Windows the agent runs its real #[cfg(windows)] code paths
                # (no Linux fallback), so exercise the tools that return
                # `unsupported` on Linux against the genuine implementations.
                if sys.platform == "win32":
                    await _assert_windows_tools(client)
                elif sys.platform.startswith("linux"):
                    await _assert_linux(client)
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


async def _call(client, tool: str, args: dict | None = None):
    """Forward a capability tool to the single "dev" agent and return its result.

    Forwarded calls require an explicit agent_id (ADR-0038); every test in this
    module targets the one real agent it spawned, so it's injected here once.
    """
    call_args = {"agent_id": "dev", **(args or {})}
    return (await client.call_tool(tool, {"args": call_args})).data


async def _assert_windows_tools(client) -> None:
    """Drive the real Windows-only tool paths (CIM/WMI, PowerShell, ipconfig).

    Each call here hits a genuine ``#[cfg(windows)]`` implementation that returns
    ``unsupported`` on the Linux build, so these assertions are meaningful only on
    a Windows runner.
    """
    # Read-only diagnostics — real data from the live machine.
    procs = await _call(client, "diag_processes")
    assert procs["processes"], "diag_processes returned no processes"
    assert all("pid" in p and "name" in p for p in procs["processes"][:5])

    # CIM Win32_Service inventory: a Windows box always runs services.
    services = await _call(client, "diag_services")
    assert services["services"], "diag_services returned no services (Win32_Service)"

    # Get-WinEvent over the System log — the real CIM/event path executed.
    eventlog = await _call(client, "diag_eventlog", {"log": "System", "count": 5})
    assert isinstance(eventlog["events"], list)

    netcfg = await _call(client, "net_config")
    assert isinstance(netcfg["interfaces"], list) and netcfg["interfaces"]
    assert "dns" in netcfg

    # Mutating but harmless: real `ipconfig /flushdns` (remote control is on by default).
    flush = await _call(client, "net_dns_flush")
    assert flush["ok"] is True

    # Boundary: shell_exec is OS-scoped to Linux/macOS. On a Windows agent the
    # server's OS guard refuses it before ever forwarding.
    shell_exec_raised = False
    try:
        await _call(client, "shell_exec", {"command": "echo hi"})
    except Exception:  # noqa: BLE001 - `unsupported` on Windows is the expected outcome
        shell_exec_raised = True
    assert shell_exec_raised, "shell_exec must be refused for a Windows agent"

    # winget/App Installer is not reliably preinstalled on hosted Server images
    # (notably windows-2022). Run it for real where present; skip cleanly when the
    # agent reports it unavailable (an `unsupported`/`exec_failed` ToolError).
    try:
        listed = await _call(client, "winget_list")
        assert isinstance(listed["packages"], list)
    except Exception as exc:  # noqa: BLE001 - winget absent is an acceptable skip
        print(f"winget_list skipped (winget unavailable on this runner): {exc}")

    # Full-fidelity, interactive-desktop tools only make sense on a real machine
    # (a self-hosted runner). Hosted runners are headless with no logged-in
    # session, so this is gated behind KENNY_E2E_FULL=1.
    if os.environ.get("KENNY_E2E_FULL") == "1":
        shot = await _call(client, "screen_capture")
        assert shot["format"] == "png"
        assert shot["image_b64"], "screen_capture returned an empty image"
        # NB: net_adapter_reset, winget_install/uninstall and agent_update are
        # deliberately NOT asserted automatically -- on a real family PC they
        # sever the network, install software, or replace the running binary.
        # Exercise those by hand on a throwaway box, not in unattended CI.


async def _assert_linux(client) -> None:
    """Drive the real Linux tool and collector paths (ADR-0031).

    The portable tools (`diag_processes`, `net_config`) run their real
    non-Windows arms; the Windows-only `winget_list` returns ``unsupported`` on the
    Linux build; and the ``#[cfg(target_os = "linux")]`` collectors report genuine
    data from dpkg, ``/etc/passwd`` and ``/proc/net``. These assertions are therefore
    meaningful only on a Linux runner.
    """
    # Read-only diagnostics — real processes from the live machine (sysinfo).
    procs = await _call(client, "diag_processes")
    assert procs["processes"], "diag_processes returned no processes"
    assert all("pid" in p and "name" in p for p in procs["processes"][:5])

    # Interfaces from sysinfo; the `dns` key is always present (empty on the
    # portable path, populated only by the Windows probe).
    netcfg = await _call(client, "net_config")
    assert isinstance(netcfg["interfaces"], list) and netcfg["interfaces"]
    assert "dns" in netcfg

    # Boundary: a Windows-only tool returns `unsupported` on the Linux build. The
    # server forwarder surfaces that error frame as a raised exception.
    winget_raised = False
    try:
        await _call(client, "winget_list")
    except Exception:  # noqa: BLE001 - `unsupported` on Linux is the expected outcome
        winget_raised = True
    assert winget_raised, "winget_list must raise `unsupported` on the Linux build"

    # Boundary: powershell_exec is OS-scoped to Windows. On a Linux agent the
    # server's OS guard refuses it before ever forwarding.
    powershell_raised = False
    try:
        await _call(client, "powershell_exec", {"script": "echo hi"})
    except Exception:  # noqa: BLE001 - `unsupported` on Linux is the expected outcome
        powershell_raised = True
    assert powershell_raised, "powershell_exec must be refused for a Linux agent"

    # Real Linux collectors, read back through the pushed snapshot.
    async def _section(name: str) -> dict:
        res = await client.call_tool("agent_snapshot", {"id": "dev", "section": name})
        return res.data["payload"]

    # dpkg inventory: an Ubuntu box always has packages installed (deterministic).
    software = await _section("installed_software")
    assert software["count"] > 0, "installed_software (dpkg) reported no packages"
    assert software["summary"] != "n/a on this platform"

    # /etc/passwd + /etc/group: root is uid 0 and therefore always an admin.
    accounts = await _section("local_accounts")
    assert accounts["count"] > 0, "local_accounts reported no accounts"
    assert "root" in accounts["admins"], "root must be listed as an admin on Linux"

    # /proc/net TCP/UDP listeners: real data, no systemd required.
    ports = await _section("listening_ports")
    assert isinstance(ports["ports"], list)
    assert ports["summary"] != "n/a on this platform"

    # systemd-backed sections: the sandbox may have no systemd bus reachable, so
    # assert only structural validity (a string `status`), not real unit/clock data.
    services = await _section("services")
    assert isinstance(services["status"], str), "services section missing string status"
    time_sync = await _section("time_sync")
    assert isinstance(time_sync["status"], str), "time_sync section missing string status"

    # The agent registered as a Linux host — Workstream D promoted `os` to a
    # first-class field that fleet_overview surfaces per agent.
    fleet = (await client.call_tool("fleet_overview", {})).data
    dev = next((a for a in fleet["agents"] if a["agent_id"] == "dev"), None)
    assert dev is not None, "agent 'dev' missing from fleet_overview"
    assert dev["os"] == "linux", f"expected os=linux, got {dev['os']!r}"
