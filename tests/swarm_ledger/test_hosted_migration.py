import base64
import copy
import gzip
import hashlib
import io
import json
import secrets
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import hosted
from tests.swarm_ledger.test_sqlite import document


@pytest.fixture
def stores(tmp_path):
    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    source.import_document("migration-proof", document())
    with source.connect() as connection, connection:
        source.save_registry(connection, "bin", {"migration-proof": 42})
    data = b"An artifact kept with its ledger."
    name = hashlib.sha256(data).hexdigest() + ".md"
    folder = source.directory / "migration-proof.media"
    folder.mkdir()
    (folder / name).write_bytes(data)
    return source, target


def test_complete_history_bin_and_files_survive_an_idempotent_import(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    result = hosted.restore(target, snapshot)
    assert target.export_document("migration-proof") == document()
    assert hosted.capture(target, "migration-proof") == snapshot
    assert hosted.restore(target, snapshot) == result
    assert result["tasks"] == 2
    assert result["events"] == 1
    assert result["revisions"] == 2
    assert result["artifacts"] == 1
    assert result["files"] == 1
    with target.connect() as connection:
        assert target.registry("bin", connection)["migration-proof"] == 42


def test_conflicting_existing_ledger_is_never_replaced(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    target.import_document("migration-proof", {**document(), "title": "Hosted work"})
    with pytest.raises(ValueError, match="conflict"):
        hosted.restore(target, snapshot)
    assert target.export_document("migration-proof")["title"] == "Hosted work"


@pytest.mark.parametrize("invalid", ["../escape.md", "a" * 64 + ".md"])
def test_invalid_file_identity_cannot_create_a_ledger(stores, invalid):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    snapshot["files"] = {invalid: base64.b64encode(b"different bytes").decode()}
    with pytest.raises(ValueError):
        hosted.restore(target, snapshot)
    assert not target.exists("migration-proof")


def test_interrupted_file_copy_can_resume_without_losing_history(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    original = hosted.write_files
    with monkeypatch.context() as patch:
        patch.setattr(hosted, "write_files", lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            hosted.restore(target, snapshot)
    assert hosted.write_files is original
    hosted.restore(target, snapshot)
    assert hosted.capture(target, "migration-proof") == snapshot


def test_fence_blocks_new_connections_and_preserves_other_ledgers(stores):
    source, _ = stores
    before = hosted.capture(source, "migration-proof")
    hosted.fence(source, "migration-proof", "https://hosted.example")
    reopened = SQLiteLedgerRepository(source.path, ledger_core)
    assert hosted.capture(reopened, "migration-proof") == before
    with pytest.raises(sqlite3.IntegrityError, match="hosted"):
        reopened.import_document("migration-proof", document(), replace=True)
    with reopened.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
        connection.execute("DELETE FROM events WHERE slug=?", ("migration-proof",))
    with reopened.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
        reopened.save_registry(connection, "bin", {})
    reopened.import_document("still-local", document())
    assert reopened.export_document("still-local") == document()
    hosted.fence(reopened, "migration-proof", "https://hosted.example")
    with pytest.raises(ValueError, match="destination"):
        hosted.fence(reopened, "migration-proof", "https://other.example")


def request(snapshot, credential):
    encoded = gzip.compress(json.dumps(snapshot).encode())
    return SimpleNamespace(
        path="/api/v1/migrations/migration-proof",
        command="POST",
        headers={
            "Host": "hosted.example",
            "X-Ledger-Migration": credential,
            "Content-Type": "application/gzip",
            "Content-Length": str(len(encoded)),
        },
        rfile=io.BytesIO(encoded),
    )


def test_expiring_import_authority_is_bound_to_the_ledger_and_scope(stores, monkeypatch):
    source, target = stores
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    credential = hosted.token(secret, "migration-proof", 1100)
    snapshot = hosted.capture(source, "migration-proof")
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    assert routes.dispatch(request(snapshot, credential), server)["data"] == hosted.manifest(snapshot)
    for invalid in (
        hosted.token(secret, "other-ledger", 1100),
        hosted.token(secret, "migration-proof", 999),
        hosted.token(secret, "migration-proof", 10000),
        "invalid",
    ):
        with pytest.raises(APIError) as failure:
            routes.dispatch(request(snapshot, invalid), server)
        assert failure.value.status == 403
    wrong = copy.deepcopy(snapshot)
    wrong["slug"] = "other-ledger"
    with pytest.raises(APIError) as failure:
        routes.dispatch(request(wrong, credential), server)
    assert failure.value.status == 400


def test_manifest_changes_when_history_or_media_changes(stores):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    expected = hosted.manifest(snapshot)
    changed = copy.deepcopy(snapshot)
    changed["state"]["_meta"]["events"][0]["unknown"]["x"] = False
    assert hosted.manifest(changed)["digest"] != expected["digest"]
    changed = copy.deepcopy(snapshot)
    changed["files"].clear()
    assert hosted.manifest(changed)["digest"] != expected["digest"]
