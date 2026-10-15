import pytest

from scripts.floor_plant import floor_plant_subject

pytestmark = pytest.mark.unit


def test_floor_plant_subject():
    assert floor_plant_subject() == 1
