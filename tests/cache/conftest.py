import pytest

from rmote.cache import JSONCacheDir, SQLiteCache
from tests.support.transports import protocol as protocol


@pytest.fixture(params=[JSONCacheDir, SQLiteCache])
def cache(request, tmp_path):
    return request.param(tmp_path / "cache")
