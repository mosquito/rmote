"""The same Agent Tool forwards local, remote, and cross-remote agents."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from rmote.protocol import Protocol
from rmote.tools import Agent, Exec


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["forward", "reverse", "remote-to-remote"])
async def test_real_agent_listing_and_signing_in_each_direction(agent, monkeypatch, direction):
    agent_path, public, _ = agent
    # In reverse mode, consulting the coordinator's environment must fail.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/does-not-exist/coordinator-agent")
    agent_env = dict(os.environ, SSH_AUTH_SOCK=str(agent_path))
    async with await Protocol.from_command(python=sys.executable, env=agent_env) as host_a:
        async with await Protocol.from_command(python=sys.executable, env=agent_env) as host_b:
            listener = None if direction == "reverse" else host_a
            endpoint = None if direction == "forward" else host_b
            explicit_path = str(agent_path) if direction == "forward" else None
            async with Agent.forward(listener=listener, agent=endpoint, path=explicit_path) as socket_path:
                assert socket_path != str(agent_path)
                env = dict(os.environ, SSH_AUTH_SOCK=socket_path)
                for args in (("ssh-add", "-L"), ("ssh-add", "-T", str(public))):
                    if listener is None:
                        process = await asyncio.create_subprocess_exec(
                            *args,
                            env=env,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        out, err = await asyncio.wait_for(process.communicate(), 5)
                        assert process.returncode == 0, err
                    else:
                        result = await listener(Exec.command, *args, env=env, capture_output=True)
                        assert result.returncode == 0, result.stderr
                        assert result.stdout is not None
                        out = result.stdout
                    if args[1] == "-L":
                        assert out.strip() == public.read_bytes().strip()
            assert not Path(socket_path).parent.exists()
    assert not Agent._sessions


@pytest.mark.asyncio
async def test_cancelled_listener_creation_releases_both_endpoints():
    started, finish = asyncio.Event(), asyncio.Event()
    paths = []

    async def invoke(method, session_id, *args):
        result = await method(session_id, *args)
        if method == Agent.listen:
            paths.append(result)
            started.set()
            await finish.wait()
        return result

    def connected(reader, writer):
        writer.close()

    with tempfile.TemporaryDirectory(prefix="rmote-agent-test-", dir="/tmp") as directory:
        path = str(Path(directory) / "source")
        source = await asyncio.start_unix_server(connected, path)
        try:

            async def use_forwarding() -> None:
                async with Agent.forward(listener=Mock(spec=Protocol, side_effect=invoke), path=path):
                    pytest.fail("Cancelled startup must not enter the context")

            opening = asyncio.create_task(use_forwarding())
            await started.wait()
            opening.cancel()
            await asyncio.sleep(0)
            assert Path(paths[0]).is_socket()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
            assert not Agent._sessions
            assert not Path(paths[0]).parent.exists()
        finally:
            source.close()
            await source.wait_closed()
