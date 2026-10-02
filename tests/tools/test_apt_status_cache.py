"""Regression tests for apt status cache."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest

from rmote.tools import apt


def test_apt_converge_does_not_reuse_stale_status(monkeypatch):
    installed: dict[str, dict[str, str]] = {}
    calls = []

    def read_status():
        return {name: dict(info) for name, info in installed.items()}

    def apt_get(*args):
        calls.append(args)
        installed["demo"] = {"Status": "install ok installed", "Version": "1"}
        return 0, "", ""

    monkeypatch.setattr(apt.Backend, "read_status", read_status)
    monkeypatch.setattr(apt.Backend, "apt_get", apt_get)
    assert apt.Apt.converge("demo")[0].changed is True
    assert apt.Apt.package("demo").changed is False
    assert calls == [("install", "demo")]


def test_apt_converge_refreshes_between_operations(monkeypatch):
    installed: dict[str, dict[str, str]] = {}
    calls = []

    def apt_get(*args):
        calls.append(args)
        if args[0] == "remove":
            installed.pop("demo")
        else:
            installed["demo"] = {"Status": "install ok installed", "Version": "1"}
        return 0, "", ""

    monkeypatch.setattr(apt.Backend, "read_status", lambda: dict(installed))
    monkeypatch.setattr(apt.Backend, "apt_get", apt_get)
    results = apt.Apt.converge("demo", "demo", apt.Package("demo", apt.State.ABSENT), "demo")
    assert [result.changed for result in results] == [True, False, True, True]
    assert [args[0] for args in calls] == ["install", "remove", "install"]
    assert apt.Apt.package("demo", apt.State.ABSENT).changed is True


def test_apt_failed_converge_does_not_keep_status(monkeypatch):
    installed: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(apt.Backend, "read_status", lambda: dict(installed))
    monkeypatch.setattr(apt.Backend, "apt_get", lambda *args: (1, "", "failed"))
    with pytest.raises(RuntimeError, match="failed"):
        apt.Apt.converge("demo")
    installed["demo"] = {"Status": "install ok installed", "Version": "2"}
    result = apt.Apt.package("demo")
    assert result.changed is False
    assert result.version == "2"


def test_apt_concurrent_converge_keeps_status_local(monkeypatch):
    state = local()
    barrier = Barrier(2)

    def read_status():
        barrier.wait(timeout=2)
        return {"demo": {"Status": "install ok installed", "Version": state.version}}

    def converge(version):
        state.version = version
        return apt.Apt.converge("demo")[0]

    monkeypatch.setattr(apt.Backend, "read_status", read_status)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(converge, ["1", "2"]))
    assert [result.version for result in results] == ["1", "2"]
    assert all(not result.changed for result in results)
