"""Keep CLI tests independent of locally stored user credentials."""

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg_data(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
