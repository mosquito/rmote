"""Real OpenSSH SFTP clients against the Python-backed mux subsystem."""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.cli.sshmux.conftest import Server


def sftp(server: Server, commands: str) -> subprocess.CompletedProcess[bytes]:
    binary = shutil.which("sftp")
    if binary is None:
        pytest.skip("requires OpenSSH sftp")
    return subprocess.run(
        [binary, "-F", "/dev/null", "-o", f"ControlPath={server.path}", "-o", "ProxyCommand=false", "-b", "-", "host"],
        input=commands.encode(),
        capture_output=True,
        timeout=20,
    )


def test_sftp_binary_transfer_metadata_directories_and_symlinks(server: Server, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "remote"
    copy = tmp_path / "copy"
    source.write_bytes(bytes(range(256)) * 8192)
    source.chmod(0o640)
    result = sftp(
        server,
        f'''mkdir "{target}"
put -p "{source}" "{target}/file"
ls -l "{target}"
rename "{target}/file" "{target}/renamed"
ln -s "renamed" "{target}/link"
get -p "{target}/link" "{copy}"
chmod 600 "{target}/renamed"
bye
''',
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert copy.read_bytes() == source.read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o640
    assert int(copy.stat().st_mtime) == int(source.stat().st_mtime)
    assert (target / "link").readlink().as_posix() == "renamed"
    assert (target / "renamed").stat().st_mode & 0o777 == 0o600
    result = sftp(server, f'rm "{target}/link"\nrm "{target}/renamed"\nrmdir "{target}"\n')
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_sftp_resume_and_fsync(server: Server, tmp_path):
    source, target, copy = (tmp_path / name for name in ("source", "target", "copy"))
    payload = bytes(range(256)) * 4096
    source.write_bytes(payload)
    target.write_bytes(payload[:98765])
    copy.write_bytes(payload[:12345])
    result = sftp(server, f'reput -f "{source}" "{target}"\nreget "{target}" "{copy}"\n')
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert target.read_bytes() == copy.read_bytes() == payload


def test_sftp_missing_file_does_not_stop_other_sessions(server: Server, tmp_path):
    result = sftp(server, f'get "{tmp_path}/missing" "{tmp_path}/out"\n')
    assert result.returncode != 0
    assert b"not found" in result.stderr or b"No such file" in result.stderr
    assert server.run("host", "printf alive").stdout == b"alive"


def test_parallel_sftp_sessions_and_shell(server: Server, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"test\0" * 100000)

    def transfer(index):
        target, copy = tmp_path / f"target{index}", tmp_path / f"copy{index}"
        result = sftp(server, f'put "{source}" "{target}"\nget "{target}" "{copy}"\n')
        assert result.returncode == 0, result.stderr
        assert copy.read_bytes() == source.read_bytes()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(transfer, i) for i in range(3)]
        assert server.run("host", "printf alive").stdout == b"alive"
        for future in futures:
            future.result()
