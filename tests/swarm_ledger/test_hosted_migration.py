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


def test_frozen_uploads_and_cleanup_preserve_fallback_files(stores, monkeypatch):
    from scripts.swarm_ledger import ledger_artifacts, ledger_media

    source, _ = stores
    monkeypatch.setattr(ledger_media.core, "LEDGER_DIR", source.directory)
    hosted.fence(source, "migration-proof", "https://hosted.example")
    folder = source.directory / "migration-proof.media"
    existing = next(folder.iterdir())
    import os

    os.utime(existing, (0, 0))
    with pytest.raises(ledger_media.Refused, match="hosted"):
        ledger_media.write_file(folder / ("a" * 64 + ".md"), b"new content")
    with pytest.raises(ledger_artifacts.media.Refused, match="hosted"):
        ledger_artifacts.store("migration-proof", "new.md", b"new content")
    ledger_media.sweep("migration-proof", set(), 10**12)
    ledger_media.purge("migration-proof")
    doc = {
        **{name: [] for name in ledger_artifacts.core.THREADS},
        "notes": [],
        "chat": [],
        "artifacts": [],
        "artifact_trash": [{"file": {"id": existing.name}, "deleted_at": 0}],
    }
    before = copy.deepcopy(doc)
    ledger_artifacts.sweep("migration-proof", doc, SimpleNamespace(at=10**12, dropped=[], dirty=False))
    assert doc == before
    assert existing.read_bytes() == b"An artifact kept with its ledger."
    ledger_media.write_file(source.directory / "another-ledger.media" / ("b" * 64 + ".md"), b"allowed")


def test_hosted_command_requires_source_fencing_before_transfer(monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(sys, "argv", ["migration", "--source", str(tmp_path), "--url", "https://hosted.example"])
    with pytest.raises(SystemExit, match="2"):
        hosted.main()


def test_migration_inventory_prints_only_the_source_manifest(stores, monkeypatch, capsys):
    import sys

    source, _ = stores
    monkeypatch.setattr(sys, "argv", ["migration", "--source", str(source.directory)])
    hosted.main()
    assert json.loads(capsys.readouterr().out) == hosted.manifest(hosted.capture(source, "migration-proof"))


def test_transfer_verifies_a_separate_read_and_blocks_redirects(stores, monkeypatch):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    secret = secrets.token_hex(32)
    expected = hosted.manifest(snapshot)
    calls = []
    answers = [expected, expected]

    def opened(req, timeout):
        calls.append((req, timeout))
        return io.BytesIO(json.dumps({"data": answers.pop(0)}).encode())

    def opener(handler):
        assert handler is hosted.NoRedirect
        assert handler().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example") is None
        return SimpleNamespace(open=opened)

    monkeypatch.setattr(hosted.urllib.request, "build_opener", opener)
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_ID", "$FIXTURE_ACCESS_ID")
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_SECRET", "$FIXTURE_ACCESS_SECRET")
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    result = hosted.transfer("https://hosted.example/", snapshot, secret)
    assert result == {"source": expected, "hosted": expected, "match": True}
    assert [req.get_method() for req, _ in calls] == ["POST", "GET"]
    assert all(timeout == 300 for _, timeout in calls)
    assert all(req.full_url == "https://hosted.example/api/v1/migrations/migration-proof" for req, _ in calls)
    first = calls[0][0]
    assert json.loads(gzip.decompress(first.data)) == snapshot
    assert first.get_header("Cf-access-client-id") == "$FIXTURE_ACCESS_ID"
    assert first.get_header("Cf-access-client-secret") == "$FIXTURE_ACCESS_SECRET"
    assert first.get_header("User-agent") == "agentihooks-swarm"
    assert hosted.authorize(secret, "migration-proof", first.get_header("X-ledger-migration"), 1000)
    answers.extend([expected, {**expected, "events": 999}])
    with pytest.raises(ValueError, match="comparison"):
        hosted.transfer("https://hosted.example", snapshot, secret)


def test_command_freezes_source_before_transferring_and_reports_comparison(stores, monkeypatch, capsys):
    import sys

    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migration",
            "--source",
            str(source.directory),
            "--url",
            "https://hosted.example",
            "--freeze",
            "--slug",
            "migration-proof",
        ],
    )

    def transfer(url, actual, supplied):
        assert url == "https://hosted.example"
        assert actual == snapshot
        assert supplied == secret
        with pytest.raises(sqlite3.IntegrityError):
            source.import_document("migration-proof", document(), replace=True)
        return {"match": True}

    monkeypatch.setattr(hosted, "transfer", transfer)
    hosted.main()
    assert json.loads(capsys.readouterr().out) == {"match": True}


def test_server_secret_is_loaded_from_openbao_only_in_memory(monkeypatch, capsys):
    secret = secrets.token_hex(32)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv("OPENBAO_URL", "https://vault.example")
    monkeypatch.setenv("OPENBAO_TOKEN", "$FIXTURE_OPENBAO_TOKEN")
    calls = []

    def opened(req, timeout):
        calls.append((req, timeout))
        return io.BytesIO(json.dumps({"data": {"data": {"SWARM_SECRET": secret}}}).encode())

    monkeypatch.setattr(hosted.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=opened))
    assert hosted.server_secret() == secret
    assert "SWARM_SECRET" not in hosted.os.environ
    assert calls[0][0].full_url == "https://vault.example/v1/secret/data/k8s/swarm"
    assert calls[0][0].get_header("X-vault-token") == "$FIXTURE_OPENBAO_TOKEN"
    assert capsys.readouterr().out == ""


def test_missing_trusted_credentials_never_freezes_the_source(stores, monkeypatch):
    import sys

    source, _ = stores
    monkeypatch.setattr(
        sys, "argv", ["migration", "--source", str(source.directory), "--url", "https://hosted.example", "--freeze"]
    )

    def missing():
        raise ValueError("trusted credentials unavailable")

    monkeypatch.setattr(hosted, "server_secret", missing, raising=False)
    with pytest.raises(ValueError, match="trusted credentials"):
        hosted.main()
    source.import_document("migration-proof", document(), replace=True)


def test_inventory_pins_every_count_and_the_complete_content_digest(stores):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert hosted.manifest(snapshot) == {
        "slug": "migration-proof",
        "revision": 1,
        "tasks": 2,
        "events": 1,
        "revisions": 2,
        "artifacts": 1,
        "artifact_trash": 1,
        "files": 1,
        "registries": 1,
        "digest": digest,
    }


def test_import_authority_expires_at_the_boundary_and_cannot_be_extended():
    secret = secrets.token_hex(32)
    assert hosted.authorize(secret, "ledger", hosted.token(secret, "ledger", 1900), 1000)
    for expires in (1000, 1901):
        assert not hosted.authorize(secret, "ledger", hosted.token(secret, "ledger", expires), 1000)
    assert not hosted.authorize("", "ledger", hosted.token("", "ledger", 1100), 1000)
    assert not hosted.authorize(secret, "ledger", None, 1000)


@pytest.mark.parametrize(
    "case,status",
    [("method", 405), ("type", 415), ("length", 400), ("large", 400), ("gzip", 400), ("json", 400), ("slug", 404)],
)
def test_migration_api_rejects_invalid_inputs_without_creating_a_ledger(stores, monkeypatch, case, status):
    source, target = stores
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(hosted.capture(source, "migration-proof"), hosted.token(secret, "migration-proof", 1100))
    if case == "method":
        incoming.command = "DELETE"
    elif case == "type":
        incoming.headers["Content-Type"] = "text/plain"
    elif case == "length":
        incoming.headers["Content-Length"] = "invalid"
    elif case == "large":
        incoming.headers["Content-Length"] = str(hosted.MAX_COMPRESSED + 1)
    elif case == "gzip":
        incoming.rfile = io.BytesIO(b"invalid gzip")
    elif case == "json":
        incoming.rfile = io.BytesIO(gzip.compress(b"{}"))
    else:
        incoming.path += "/unexpected"
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    with pytest.raises(APIError) as failure:
        routes.dispatch(incoming, server)
    assert failure.value.status == status
    assert not target.exists("migration-proof")


def test_migration_readback_reports_the_actual_hosted_document(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    hosted.restore(target, snapshot)
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(snapshot, hosted.token(secret, "migration-proof", 1100))
    incoming.command = "GET"
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    assert routes.dispatch(incoming, server) == {"data": hosted.manifest(snapshot)}


def test_source_inventory_refuses_symbolic_links_and_missing_databases(stores, tmp_path):
    source, _ = stores
    folder = source.directory / "migration-proof.media"
    existing = next(folder.iterdir())
    (folder / ("a" * 64 + ".md")).symlink_to(existing)
    with pytest.raises(ValueError, match="regular files"):
        hosted.capture(source, "migration-proof")
    with pytest.raises(ValueError, match="database"):
        hosted.capture(SQLiteLedgerRepository(tmp_path / "missing" / DATABASE, ledger_core), "migration-proof")


def test_import_refuses_corrupt_files_and_cross_ledger_registry_entries(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    name = next(iter(snapshot["files"]))
    corrupt = copy.deepcopy(snapshot)
    corrupt["files"][name] = "!bad-base64!"
    with pytest.raises(ValueError):
        hosted.restore(target, corrupt)
    corrupt = copy.deepcopy(snapshot)
    corrupt["registries"][0][1] = "another-ledger"
    with pytest.raises(ValueError, match="registry"):
        hosted.restore(target, corrupt)
    folder = target.directory / "migration-proof.media"
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"hosted file must remain")
    with pytest.raises(ValueError, match="conflict"):
        hosted.restore(target, snapshot)
    assert (folder / name).read_bytes() == b"hosted file must remain"
    assert not target.exists("migration-proof")


def test_unknown_source_ledger_cannot_be_fenced(stores):
    source, _ = stores
    with pytest.raises(ValueError, match="no such ledger"):
        hosted.fence(source, "absent", "https://hosted.example")
