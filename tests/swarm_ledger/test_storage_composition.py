import ast
from pathlib import Path

import pytest

import scripts.swarm_ledger.repository as package
from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import LedgerRepository
from tests.swarm_ledger.test_sqlite import document

ROOT = Path(__file__).resolve().parents[2]
STORE = "scripts.swarm_ledger.repository.sqlite"
COMPOSITION_ROOT = ROOT / "scripts" / "swarm_ledger" / "storage.py"
STORE_PACKAGE = ROOT / "scripts" / "swarm_ledger" / "repository"


def imported(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def store_importers() -> list:
    sources = [*(ROOT / "scripts").rglob("*.py"), *(ROOT / "hooks").rglob("*.py")]
    allowed = [path for path in sources if path == COMPOSITION_ROOT or STORE_PACKAGE in path.parents]
    return sorted(str(path.relative_to(ROOT)) for path in sources if path not in allowed and STORE in imported(path))


def test_no_module_outside_the_composition_root_imports_the_sqlite_store():
    assert store_importers() == []


def test_the_import_check_sees_an_absolute_import_of_the_store(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from scripts.swarm_ledger.repository import sqlite\n", encoding="utf-8")
    assert STORE in imported(probe)


def test_the_repository_package_binds_no_repository_at_import():
    assert "repository" not in vars(package)


def test_the_composition_root_builds_one_repository_per_process():
    built = storage.default()
    assert storage.default() is built
    assert isinstance(built, LedgerRepository)


def test_a_built_repository_stores_at_the_path_it_was_given(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    built.import_document("composed", document())
    assert built.path == tmp_path / storage.DATABASE
    assert storage.read_document(tmp_path, "composed") == document()


def test_the_relay_journal_holds_releases_and_drops_revisions(tmp_path):
    journal = storage.build(tmp_path / storage.DATABASE)
    assert journal.held_revisions() == {}
    for kind, slug, revision in (("relay", "a", 3), ("relay", "a", 9), ("relay", "a", 3), ("alerts", "b", 4)):
        journal.hold_revision(kind, slug, revision)
    assert journal.held_revisions() == {"relay": {"a": [3, 9]}, "alerts": {"b": [4]}}
    journal.release_revisions("relay", "a", 1, 5)
    assert journal.held_revisions() == {"relay": {"a": [9]}, "alerts": {"b": [4]}}
    journal.drop_revisions("alerts", "b")
    assert journal.held_revisions() == {"relay": {"a": [9]}}


def test_a_failed_transaction_leaves_nothing_stored(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    with pytest.raises(RuntimeError, match="^stop$"), built.transaction() as transaction:
        transaction.insert("rolled", document(), None)
        assert transaction.exists("rolled")
        raise RuntimeError("stop")
    assert not built.exists("rolled")


def test_a_transaction_holds_the_core_lock(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    with built.transaction():
        assert built.domain.LOCK.locked()
    assert not built.domain.LOCK.locked()


def test_a_snapshot_of_a_folder_without_a_database_is_none(tmp_path):
    with storage.build(tmp_path / "absent" / storage.DATABASE).snapshot() as snapshot:
        assert snapshot is None


def test_a_snapshot_exports_the_stored_document_with_its_registries(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    built.import_document("snapped", document())
    with built.transaction() as transaction:
        transaction.add_registries([["bin", "snapped", "1"]])
    with built.snapshot() as snapshot:
        assert snapshot.export("snapped") == document()
        assert snapshot.registries("snapped") == [["bin", "snapped", "1"]]
