import fakeredis
import pytest

pytestmark = pytest.mark.xdist_group(name="fakeredis")


def test_eager_redis_import_plant():
    assert fakeredis.FakeRedis
