import sqlite3

import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import shared
from tests.swarm_ledger.test_sqlite import document

pytestmark = pytest.mark.unit

SLUG = "conversion-proof"
COUNTS = {"tasks": 2, "events": 1, "comments": 1, "artifacts": 1}


@pytest.fixture
def stores(tmp_path):
    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    source.import_document(SLUG, document(), token="kept-token")
    with source.connect() as connection, connection:
        source.save_registry(connection, "bin", {SLUG: 42})
    return source, target


def generation(repo, slug):
    with repo.connect() as connection:
        return connection.execute("SELECT generation FROM ledgers WHERE slug=?", (slug,)).fetchone()[0]


def test_counts_name_tasks_events_comments_and_artifacts_of_one_document():
    assert shared.counts(document()) == COUNTS
    nested = {"_meta": {"events": [], "comments": [1]}, "notes": [{"comments": [{"comments": [1, 2]}]}]}
    assert shared.counts(nested) == {"tasks": 0, "events": 0, "comments": 3, "artifacts": 0}


def test_a_conversion_copies_full_history_token_and_registries_then_fences_the_source(stores):
    source, target = stores
    report = shared.convert(source, target)
    assert report == {
        "ledgers": [{"slug": SLUG, "source": COUNTS, "shared": COUNTS, "match": True}],
        "totals": {"ledgers": 1, "source": COUNTS, "shared": COUNTS},
        "match": True,
        "read_only": True,
        "dry_run": False,
    }
    assert target.export_document(SLUG) == document()
    assert target.token(SLUG) == "kept-token"
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 42
    assert sqlite.fenced(source.directory, "*")
    with pytest.raises(sqlite3.IntegrityError, match="ledger moved to hosted service"):
        source.import_document("after", document())


def test_an_equal_copy_already_in_the_shared_database_is_left_untouched(stores):
    source, target = stores
    shared.convert(source, target)
    before = generation(target, SLUG)
    assert shared.convert(source, target)["match"] is True
    assert generation(target, SLUG) == before


def test_a_write_landing_before_the_fence_reaches_the_shared_database(stores, monkeypatch):
    source, target = stores
    freeze = shared.freeze

    def late_write(repo):
        changed = {**document(), "title": "Late write", "tasks": [*document()["tasks"], {"id": "third"}]}
        repo.import_document(SLUG, changed, token="kept-token", replace=True)
        repo.import_document("late-ledger", document())
        freeze(repo)

    monkeypatch.setattr(shared, "freeze", late_write)
    report = shared.convert(source, target)
    assert report["match"] is True
    assert [row["slug"] for row in report["ledgers"]] == ["conversion-proof", "late-ledger"]
    assert target.export_document(SLUG)["title"] == "Late write"
    assert report["ledgers"][0]["shared"]["tasks"] == 3
    assert target.export_document("late-ledger") == document()


def test_a_registry_change_alone_before_the_fence_reaches_the_shared_database(stores, monkeypatch):
    source, target = stores
    freeze = shared.freeze

    def binned(repo):
        with repo.connect() as connection, connection:
            repo.save_registry(connection, "bin", {SLUG: 7})
        freeze(repo)

    monkeypatch.setattr(shared, "freeze", binned)
    assert shared.convert(source, target)["match"] is True
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 7


def test_a_dry_run_reports_counts_and_rolls_every_write_back(stores):
    source, target = stores
    report = shared.convert(source, target, dry_run=True)
    assert report["ledgers"] == [{"slug": SLUG, "source": COUNTS, "shared": COUNTS, "match": True}]
    assert report["dry_run"] is True
    assert report["read_only"] is False
    assert not target.exists(SLUG)
    assert not sqlite.fenced(source.directory, "*")
    source.import_document("still-writable", document())


def test_a_differing_count_is_reported_as_no_match(stores, monkeypatch):
    source, target = stores
    original = shared.counts
    calls = []

    def skewed(state):
        calls.append(state)
        result = original(state)
        return {**result, "events": result["events"] + 1} if len(calls) % 2 == 0 else result

    monkeypatch.setattr(shared, "counts", skewed)
    report = shared.convert(source, target, dry_run=True)
    assert report["match"] is False
    assert report["ledgers"][0]["match"] is False


def test_the_command_refuses_without_a_shared_database(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(tmp_path)])
    with pytest.raises(SystemExit, match="AGENTIHOOKS_SWARM_DATABASE_URL"):
        shared.main()
