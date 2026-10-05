"""Disposable systemd host for collector tests, scoped to this directory."""

import asyncio
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from rmote.process import async_process
from rmote.protocol import Protocol
from tests.support.transports import pacman_docker_protocol as pacman_docker_protocol


@pytest.fixture(scope="session")
def facts_systemd_image(tool_docker: str, request: pytest.FixtureRequest) -> str:
    base = getattr(request, "param", "debian:forky-slim")
    image = "rmote-facts-tests:" + base.replace(":", "-")
    subprocess.run(
        [
            tool_docker,
            "build",
            "--build-arg",
            f"BASE={base}",
            "-t",
            image,
            str(Path(__file__).parent / "docker"),
        ],
        check=True,
        timeout=300,
    )
    return image


@pytest_asyncio.fixture
async def facts_docker_protocol(tool_docker: str, facts_systemd_image: str) -> AsyncIterator[Protocol]:
    name = "rmote-facts-" + uuid.uuid4().hex[:12]
    created = False
    try:
        await async_process(
            tool_docker,
            "run",
            "-d",
            "--name",
            name,
            "--privileged",
            "--cgroupns=private",
            "--network=none",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            "--sysctl",
            "net.ipv6.conf.all.disable_ipv6=0",
            "--sysctl",
            "net.ipv6.conf.default.disable_ipv6=0",
            facts_systemd_image,
            check=True,
            capture_output=True,
            timeout=30,
        )
        created = True
        async with asyncio.timeout(30):
            while True:
                boot = await async_process(
                    tool_docker,
                    "exec",
                    name,
                    "systemctl",
                    "is-system-running",
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if boot.stdout.strip() in {"running", "degraded"}:
                    break
                await asyncio.sleep(0.1)
        commands = [
            # Match systemd's container contract: no udev is running here.
            ("mount", "-o", "remount,ro", "/sys"),
            ("ip", "link", "add", "facts0", "type", "dummy"),
            ("ip", "link", "set", "facts0", "up"),
            ("ip", "address", "add", "192.0.2.10/24", "dev", "facts0"),
            ("ip", "-6", "address", "add", "2001:db8::10/64", "dev", "facts0", "nodad"),
            ("ip", "rule", "add", "priority", "12345", "from", "192.0.2.10", "table", "100"),
            ("ip", "-6", "rule", "add", "priority", "12345", "from", "2001:db8::10", "table", "100"),
            ("ip", "route", "add", "192.0.2.0/24", "dev", "facts0", "table", "100"),
            ("ip", "-6", "route", "add", "2001:db8::/64", "dev", "facts0", "table", "100"),
            ("systemctl", "start", "systemd-resolved.service", "systemd-networkd.service"),
            ("/usr/lib/systemd/systemd-networkd-wait-online", "--interface=facts0:routable", "--timeout=10"),
        ]
        for command in commands:
            await async_process(tool_docker, "exec", name, *command, check=True, capture_output=True, timeout=15)
        with tempfile.TemporaryFile() as stderr:
            child = await asyncio.create_subprocess_exec(
                tool_docker,
                "exec",
                "-i",
                name,
                "python3",
                "-qui",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
            try:
                async with await Protocol.from_subprocess(child) as protocol:
                    yield protocol
            finally:
                if child.returncode is None:
                    child.terminate()
                await child.wait()
    except Exception as exc:
        logs = await async_process(tool_docker, "logs", name, capture_output=True, text=True, timeout=5)
        exc.add_note(logs.stdout + logs.stderr)
        raise
    finally:
        await async_process(tool_docker, "rm", "-f", name, check=created, capture_output=True, timeout=30)
