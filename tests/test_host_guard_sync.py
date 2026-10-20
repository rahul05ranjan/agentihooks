from pathlib import Path

import pytest

from scripts.host_guard import sync

pytestmark = pytest.mark.unit

LINKS = ("guard-daemon.sh", "guard-config.json")
UNITS = ("guard-daemon.service", "guard-daemon.timer")


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake(*args):
        recorded.append(args)
        return True

    monkeypatch.setattr(sync, "_systemctl", fake)
    return recorded


def _units(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _synced(home: Path) -> None:
    for name in LINKS:
        (home / name).symlink_to(sync.REPO / name)
    _units(home).mkdir(parents=True)
    for name in UNITS:
        (_units(home) / name).write_text((sync.REPO / name).read_text())


def test_the_repository_ships_both_units_running_the_home_script():
    assert "ExecStart=%h/guard-daemon.sh" in (sync.REPO / "guard-daemon.service").read_text()
    assert "OnUnitActiveSec=60" in (sync.REPO / "guard-daemon.timer").read_text()


def test_a_synced_home_reports_no_drift(tmp_path):
    _synced(tmp_path)
    assert sync.drift(tmp_path) == []


def test_a_hand_copied_config_is_drift_even_when_its_text_matches(tmp_path):
    _synced(tmp_path)
    config = tmp_path / "guard-config.json"
    config.unlink()
    config.write_text((sync.REPO / "guard-config.json").read_text())
    assert sync.drift(tmp_path) == [f"{config} is not a link to {sync.REPO / 'guard-config.json'}"]


def test_a_link_to_another_copy_and_a_missing_script_are_drift(tmp_path):
    _synced(tmp_path)
    other = tmp_path / "other.json"
    other.write_text("{}")
    (tmp_path / "guard-config.json").unlink()
    (tmp_path / "guard-config.json").symlink_to(other)
    (tmp_path / "guard-daemon.sh").unlink()
    assert sync.drift(tmp_path) == [
        f"{tmp_path / 'guard-daemon.sh'} is missing",
        f"{tmp_path / 'guard-config.json'} is not a link to {sync.REPO / 'guard-config.json'}",
    ]


def test_a_changed_or_missing_unit_is_drift(tmp_path):
    _synced(tmp_path)
    service = _units(tmp_path) / "guard-daemon.service"
    service.write_text("[Service]\nExecStart=/elsewhere\n")
    (_units(tmp_path) / "guard-daemon.timer").unlink()
    assert sync.drift(tmp_path) == [
        f"{service} differs from {sync.REPO / 'guard-daemon.service'}",
        f"{_units(tmp_path) / 'guard-daemon.timer'} is missing",
    ]


def test_install_links_both_files_and_writes_both_units_into_an_empty_home(tmp_path, calls):
    messages = sync.install(tmp_path)
    for name in LINKS:
        assert (tmp_path / name).is_symlink()
        assert (tmp_path / name).readlink() == sync.REPO / name
    for name in UNITS:
        assert (_units(tmp_path) / name).read_text() == (sync.REPO / name).read_text()
    assert sync.drift(tmp_path) == []
    assert messages == [f"[OK] Host guard installed in {tmp_path} from {sync.REPO}"]
    assert calls == []


def test_install_replaces_a_stale_hand_copied_config_and_a_stale_unit(tmp_path, calls):
    _synced(tmp_path)
    config = tmp_path / "guard-config.json"
    config.unlink()
    config.write_text('{"excludes": []}')
    (_units(tmp_path) / "guard-daemon.timer").write_text("[Timer]\n")
    sync.install(tmp_path)
    assert config.is_symlink() and config.readlink() == sync.REPO / "guard-config.json"
    assert (_units(tmp_path) / "guard-daemon.timer").read_text() == (sync.REPO / "guard-daemon.timer").read_text()
    assert sync.drift(tmp_path) == []


def test_install_reloads_and_enables_the_timer_only_in_the_live_home(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(sync, "_live", lambda home: home == tmp_path)
    sync.install(tmp_path)
    assert calls == [("daemon-reload",), ("enable", "--now", "guard-daemon.timer")]


def test_the_live_home_is_the_account_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_account_home", lambda: tmp_path / "link")
    (tmp_path / "link").symlink_to(tmp_path)
    assert sync._live(tmp_path) is True
    assert sync._live(tmp_path / "other") is False


def test_reconcile_leaves_a_home_without_a_guard_alone(tmp_path, calls):
    assert sync.reconcile(tmp_path) == "[--] Host guard not installed; agentihooks host-guard install adds it"
    assert list(tmp_path.iterdir()) == []


def test_reconcile_brings_an_installed_guard_back_to_the_repository(tmp_path, calls):
    _units(tmp_path).mkdir(parents=True)
    (_units(tmp_path) / "guard-daemon.timer").write_text("[Timer]\n")
    (tmp_path / "guard-config.json").write_text('{"excludes": []}')
    assert sync.reconcile(tmp_path) == f"[OK] Host guard installed in {tmp_path} from {sync.REPO}"
    assert sync.drift(tmp_path) == []


def test_reconcile_reports_a_guard_already_in_sync(tmp_path, calls):
    _synced(tmp_path)
    assert sync.reconcile(tmp_path) == f"[OK] Host guard in sync with {sync.REPO}"
    assert calls == []


def test_check_exits_one_and_names_the_drift_then_zero_after_install(tmp_path, monkeypatch, capsys, calls):
    monkeypatch.setattr(sync.Path, "home", lambda: tmp_path)
    (tmp_path / "guard-config.json").write_text("{}")
    assert sync.main(["check"]) == 1
    out = capsys.readouterr().out
    assert f"{tmp_path / 'guard-config.json'} is not a link to {sync.REPO / 'guard-config.json'}" in out
    assert sync.main(["install"]) == 0
    assert capsys.readouterr().out == f"[OK] Host guard installed in {tmp_path} from {sync.REPO}\n"
    assert sync.main(["check"]) == 0
    assert capsys.readouterr().out == f"[OK] Host guard in sync with {sync.REPO}\n"
