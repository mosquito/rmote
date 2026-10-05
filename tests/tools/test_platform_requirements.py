"""A tool that needs Linux or a program must name it, not fail on a path.

The failure travels through RPC, where the caller sees the exception and
nothing else. A missing file or a missing program reads like a damaged host;
these messages name the reason instead.
"""

import platform
import shutil
from pathlib import Path

import pytest

from rmote.requires import needs_file, needs_linux, needs_program
from rmote.tools.apt import Apt
from rmote.tools.apt_repository import AptRepository
from rmote.tools.pacman import Pacman
from rmote.tools.pacman_repository import PacmanRepository
from rmote.tools.service import Service
from rmote.tools.sysctl import Sysctl
from rmote.tools.user import User

pytestmark = pytest.mark.timeout(30)


class TestChecks:
    def test_a_kernel_interface_names_linux_and_the_system_found(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        with pytest.raises(NotImplementedError) as failure:
            needs_linux("Sysctl.get", "/proc/sys")
        assert str(failure.value) == "Sysctl.get needs the /proc/sys interface of Linux, and this host runs Darwin"

    def test_linux_passes_the_check(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        # A target that has the interface passes, so the call raises nothing.
        needs_linux("Sysctl.get", "/proc/sys")

    def test_an_absent_program_names_what_provides_it(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(NotImplementedError) as failure:
            needs_program("Service.start", "systemctl", "systemd")
        assert str(failure.value) == "Service.start needs the systemctl program of systemd, and this host has none"

    def test_a_program_on_the_path_passes_the_check(self):
        # Every POSIX target has a shell, so the call raises nothing.
        needs_program("Exec.command", "sh", "POSIX")

    def test_an_absent_configuration_names_whose_it_is(self, tmp_path: Path):
        with pytest.raises(NotImplementedError) as failure:
            needs_file("AptRepository.absent", tmp_path / "apt", "Debian and Ubuntu")
        assert "of Debian and Ubuntu, and this host has none" in str(failure.value)
        # The directory that exists passes, so the call raises nothing.
        needs_file("AptRepository.absent", tmp_path, "Debian and Ubuntu")


class TestToolsRefuse:
    """The refusal must arrive through the tool, not only from the check."""

    def test_sysctl_names_the_interface(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        with pytest.raises(NotImplementedError, match="Sysctl needs the /proc/sys interface of Linux"):
            Sysctl.get("net.ipv4.ip_forward")

    @pytest.mark.asyncio
    async def test_service_names_systemd(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(NotImplementedError, match="Service needs the systemctl program of systemd"):
            await Service.status("nginx")

    @pytest.mark.asyncio
    async def test_apt_names_debian(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(NotImplementedError, match="Apt needs the apt-get program of Debian and Ubuntu"):
            await Apt.update()

    @pytest.mark.asyncio
    async def test_pacman_names_arch(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(NotImplementedError, match="Pacman needs the pacman program of Arch Linux"):
            await Pacman.update()

    @pytest.mark.asyncio
    async def test_user_names_the_shadow_utilities(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(NotImplementedError, match="User needs the useradd program of the shadow utilities"):
            await User.present("nobody-at-all")

    def test_apt_repository_names_its_configuration(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("rmote.tools.apt_repository._SOURCES_DIR", tmp_path / "apt" / "sources.list.d")
        with pytest.raises(NotImplementedError, match="of Debian and Ubuntu"):
            AptRepository.absent("example")

    def test_pacman_repository_names_its_configuration(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("rmote.tools.pacman_repository._PACMAN_CONF", tmp_path / "pacman.conf")
        with pytest.raises(NotImplementedError, match="of Arch Linux"):
            PacmanRepository.absent("example")
