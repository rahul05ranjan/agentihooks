import pytest

from scripts.profiles import master_login, render
from tests.test_profile_render import _write
from tests.test_profile_render import world as world


@pytest.fixture
def roles(world):
    profiles = world["bundle"] / "profiles"
    for name in ("master", "engineer", "cicd", "planner"):
        _write(profiles / name / "profile.yml", f"name: {name}\n")
    _write(profiles / "rb-master" / "profile.yml", "name: rb-master\nextends: [master]\n")
    return world


def test_the_login_file_lives_in_its_own_folder_under_the_agentihooks_home(roles):
    assert master_login.directory() == roles["home"] / ".agentihooks" / "master-login"
    assert master_login.credentials() == master_login.directory() / ".credentials.json"


@pytest.mark.parametrize(
    ("chain", "master"),
    [
        (["master"], True),
        (["package:master"], True),
        (["rb-master", "master"], True),
        (["engineer"], False),
        ([], False),
    ],
)
def test_a_chain_is_master_when_any_layer_is_the_master_role(chain, master):
    assert master_login.is_master(chain) is master


@pytest.mark.parametrize("name", ["master", "rb-master"])
def test_only_master_homes_link_the_master_login(roles, name):
    out = render.render_claude(name)
    link = out / ".credentials.json"
    assert link.is_symlink() and link.readlink() == master_login.credentials()
    assert master_login.directory().stat().st_mode & 0o777 == 0o700
    assert render.render_claude(name) is None


@pytest.mark.parametrize("name", ["engineer", "cicd", "planner", "rb-role"])
def test_engineer_ci_and_planner_homes_carry_no_claude_login(roles, name):
    (roles["home"] / ".claude" / ".credentials.json").write_text("operator login")
    out = render.render_claude(name)
    assert not (out / ".credentials.json").exists() and not (out / ".credentials.json").is_symlink()
    assert not master_login.directory().exists()
    assert render.render_claude(name) is None


def test_a_worker_home_still_linking_the_login_is_rendered_again_without_it(roles):
    out = render.render_claude("engineer")
    (out / ".credentials.json").symlink_to(roles["home"] / ".claude" / ".credentials.json")
    assert render.render_claude("engineer") is not None
    assert not (render.profile_dir("engineer") / "claude" / ".credentials.json").is_symlink()


def test_a_master_home_without_its_link_is_rendered_again_with_it(roles):
    out = render.render_claude("master")
    (out / ".credentials.json").unlink()
    (out / ".credentials.json").symlink_to(roles["home"] / ".claude" / ".credentials.json")
    assert render.render_claude("master") is not None
    fresh = render.profile_dir("master") / "claude" / ".credentials.json"
    assert fresh.readlink() == master_login.credentials()
