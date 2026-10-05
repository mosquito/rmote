"""Readable host documents retain safe filenames and atomic cache operations."""

import json

from rmote.cache import CacheEntry, JSONCacheDir
from rmote.cache.backends import JSONEntryCodec
from tests.cache.models import Payload


def test_host_filenames_and_indentation(tmp_path):
    cache = JSONCacheDir(tmp_path / "cache")
    hosts = {
        "server.example": "server.example",
        "admin@192.168.17.58": "admin@192.168.17.58",
        "admin@[2001:db8::1]": "admin@[2001:db8::1]",
        "../host": "..%2Fhost",
        "..%2Fhost": "..%252Fhost",
        "host\\path": "host%5Cpath",
    }
    for host, name in hosts.items():
        payload = json.loads(JSONEntryCodec.dumps(CacheEntry(Payload({"host": host}), 1)))
        cache.write_entry(host, "system", json.dumps(payload))
        text = (cache.path / f"{name}.json").read_text()
        assert text == json.dumps({"system": payload}, allow_nan=False, indent=1)
        assert (cache.path / f"{name}.lock").is_file()
    for host, name in hosts.items():
        entry = cache.read_entry(host, "system")
        assert entry is not None and entry.data == Payload({"host": host})
        cache.write_entry(host, "system", None)
        assert json.loads((cache.path / f"{name}.json").read_text()) == {}
    assert not (tmp_path / "host.json").exists()
