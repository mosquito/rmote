import pytest

from rmote.cache import JSONCacheDir, SQLiteCache
from tests.support.transports import docker as docker
from tests.support.transports import docker_image as docker_image
from tests.support.transports import docker_protocol as docker_protocol
from tests.support.transports import protocol as protocol


@pytest.fixture(params=[JSONCacheDir, SQLiteCache])
def cache(request, tmp_path):
    return request.param(tmp_path / "cache")
