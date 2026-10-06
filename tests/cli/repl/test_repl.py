import json
import os
import re
import signal
import subprocess
import sys
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

from rmote.cli import build_parser
from rmote.cli.repl import wake_on_termination
from tests.cli.repl.conftest import Session


def run_script(
    source: str, *, asynchronous: bool = False, args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rmote", "repl", *(["--async"] if asynchronous else []), *args],
        input=source,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_ready_namespace_facts_and_interactive_tool(asynchronous: bool) -> None:
    prefix = "await " if asynchronous else ""
    result = run_script(
        "import os, json\n"
        "class Probe(Tool):\n"
        "    @staticmethod\n"
        "    def pid():\n"
        "        import os\n"
        "        return os.getpid()\n"
        f"pid = {prefix}remote(Probe.pid)\n"
        "assert pid != os.getpid()\n"
        f"again = {prefix}remote(Probe.pid)\n"
        "assert again == pid\n"
        "assert host['system'].hostname\n"
        "assert host['python'].executable == __import__('sys').executable\n"
        "assert rmote.__name__ == 'rmote'\n"
        "assert FileSystem and Exec and process\n"
        "print(json.dumps([type(remote).__name__, pid]))\n",
        asynchronous=asynchronous,
    )
    assert result.returncode == 0, result.stderr
    kind, pid = json.loads(result.stdout)
    assert kind == ("Protocol" if asynchronous else "Connection")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["1 / 0", "if =", "raise SystemExit(7)"])
def test_script_failure_status(asynchronous: bool, source: str) -> None:
    result = run_script(source, asynchronous=asynchronous)
    assert result.returncode == (7 if "SystemExit" in source else 1)


def test_sync_rejects_top_level_await() -> None:
    result = run_script("await asyncio.sleep(0)")
    assert result.returncode == 1
    assert "SyntaxError" in result.stderr


@pytest.mark.parametrize("asynchronous", [False, True])
def test_explicit_transport_receives_python_flags_and_env(asynchronous: bool, tmp_path) -> None:
    wrapper = tmp_path / "transport.py"
    wrapper.write_text(
        "import os, sys\n"
        "assert sys.argv[1] == '--debug'\n"
        "assert sys.argv[-1] == '-qui'\n"
        "os.environ['RMOTE_REPL_TRANSPORT'] = 'passed'\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n"
    )
    prefix = "await " if asynchronous else ""
    result = run_script(
        "",
        asynchronous=asynchronous,
        args=(
            "--python",
            sys.executable,
            "-c",
            f"print(({prefix}remote(Exec.command, 'printenv', 'RMOTE_REPL_TRANSPORT', capture_output=True)).stdout.decode().strip())",
            "--",
            sys.executable,
            str(wrapper),
            "--debug",
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_bootstrap_failure_is_readable(asynchronous: bool) -> None:
    result = run_script("", asynchronous=asynchronous, args=("--", "sh", "-c", "echo transport-failed >&2; exit 3"))
    assert result.returncode == 1
    assert "transport-failed" in result.stderr
    assert "Traceback" not in result.stderr


def test_parser_keeps_transport_arguments() -> None:
    args = build_parser().parse_args(["repl", "--async", "--python", "pypy3", "--", "ssh", "host", "--debug"])
    assert args.asynchronous is True
    assert args.python == "pypy3"
    assert args.transport == ["--", "ssh", "host", "--debug"]


def test_terminal_tool_multiline_errors_interrupt_and_eof(session: Session) -> None:
    asynchronous = session.asynchronous
    prefix = "await " if asynchronous else ""
    session.send("import os")
    output = session.send(
        "class Probe(Tool):\n"
        "    @staticmethod\n"
        "    def pid():\n"
        "        import os\n"
        "        return os.getpid()\n"
        "    @staticmethod\n"
        "    async def wait():\n"
        "        import asyncio\n"
        "        await asyncio.sleep(60)\n"
    )
    assert b"Traceback" not in output
    output = session.send(f"print('PID', {prefix}remote(Probe.pid))")
    pid = int(re.search(rb"PID (\d+)", output)[1])  # type: ignore[index]
    assert pid != session.process.pid
    assert b"ZeroDivisionError" in session.send("1 / 0")
    assert b"SyntaxError" in session.send("if =")
    if asynchronous:
        session.send("async def twice():\n    return await remote(Probe.pid)\n")
        assert str(pid).encode() in session.send("await twice()")
        session.send("task = asyncio.create_task(asyncio.sleep(0.01, result=123))")
        # Reading the next line must leave the protocol event loop running.
        assert b"123" in session.send("await task")
    os.write(session.master, f"print('WAITING', flush=True); {prefix}remote(Probe.wait)\n".encode())
    session.until(b"\r\nWAITING\r\n")
    os.write(session.master, b"\x03")
    assert b"KeyboardInterrupt" in session.until(b">>> ")
    assert str(pid).encode() in session.send(f"{prefix}remote(Probe.pid)")
    session.process.send_signal(signal.SIGINT)
    session.until(b">>> ")
    # libedit's default Ctrl-D action includes completion. Select its explicit
    # EOF action to test Python's EOF handling independently of the keymap.
    session.send(
        "import readline; readline.parse_and_bind('bind ^D ed-end-of-file') if 'libedit' in readline.__doc__ else None"
    )
    os.write(session.master, b"\x04")
    assert session.wait() == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("delivery", ["process", "pending"])
def test_sigterm_reaps_transport(session: Session, tmp_path: Path, delivery: str) -> None:
    prefix = "await " if session.asynchronous else ""
    session.send(
        "class Probe(Tool):\n    @staticmethod\n    def pid():\n        import os\n        return os.getpid()\n"
    )
    output = session.send(f"print('PID', {prefix}remote(Probe.pid))")
    pid = int(re.search(rb"PID (\d+)", output)[1])  # type: ignore[index]
    if delivery == "pending":
        # Reproduce a pending Python handler without interrupting readline's
        # syscall. Real process signals race with entry into that syscall.
        control = tmp_path / "terminate"
        os.mkfifo(control)
        session.send("import _thread, signal, threading")
        session.send(
            "def terminate_from_thread():\n"
            f"    with open({str(control)!r}, 'rb') as control:\n"
            "        control.read(1)\n"
            "    _thread.interrupt_main(signal.SIGTERM)\n"
        )
        session.send("threading.Thread(target=terminate_from_thread, daemon=True).start()")
        with control.open("wb") as handle:
            handle.write(b"!")
    else:
        session.process.terminate()
    assert session.wait() == 143
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("fail", [False, True])
def test_termination_wakeup_restores_process_state(fail: bool) -> None:
    previous_handler = signal.getsignal(signal.SIGTERM)
    reader, writer = os.pipe()
    os.set_blocking(writer, False)
    previous_fd = signal.set_wakeup_fd(writer)
    try:
        with pytest.raises(ValueError) if fail else nullcontext():
            with wake_on_termination():
                if fail:
                    raise ValueError("console failed")
        assert signal.getsignal(signal.SIGTERM) == previous_handler
        assert signal.set_wakeup_fd(writer) == writer
        assert not any(thread.name == "rmote-repl-signals" for thread in threading.enumerate())
    finally:
        signal.set_wakeup_fd(previous_fd)
        os.close(writer)
        os.close(reader)
