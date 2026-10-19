import gzip
import hashlib
import io
import json
import secrets
import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import hosted
from tests.swarm_ledger.test_sqlite import document

SLUG = "refresh-proof"


def media(repo, data):
    name = hashlib.sha256(data).hexdigest() + ".md"
    folder = repo.directory / f"{SLUG}.media"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return name


def digest(snapshot):
    return hosted.manifest(snapshot)["digest"]


@pytest.fixture
def diverged(tmp_path):
    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    source.import_document(SLUG, document())
    with source.connect() as connection, connection:
        source.save_registry(connection, "bin", {SLUG: 42})
    media(source, b"Kept on both sides.")
    hosted.restore(target, hosted.capture(source, SLUG))
    target.import_document(SLUG, {**document(), "title": "Hosted work"}, replace=True)
    media(target, b"Only the hosted copy holds this.")
    with target.connect() as connection, connection:
        target.save_registry(connection, "bin", {SLUG: 7})
    source.import_document(SLUG, {**document(), "title": "Source work"}, replace=True)
    media(source, b"Only the source holds this.")
    return source, target


def archived(target, snapshot):
    path = target.directory / f"{SLUG}.displaced" / f"{digest(snapshot)}.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def test_refresh_replaces_the_expected_hosted_copy_and_archives_it(diverged):
    source, target = diverged
    snapshot = hosted.capture(source, SLUG)
    displaced = hosted.capture(target, SLUG)
    token = target.token(SLUG)
    hosted_only = hashlib.sha256(b"Only the hosted copy holds this.").hexdigest() + ".md"
    assert hosted_only in displaced["files"]
    expected = (digest(snapshot), digest(displaced))
    result = hosted.restore(target, snapshot, expected)
    assert result == hosted.manifest(snapshot)
    assert hosted.capture(target, SLUG) == snapshot
    assert target.export_document(SLUG)["title"] == "Source work"
    assert target.token(SLUG) == token
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 42
    assert not (target.directory / f"{SLUG}.media" / hosted_only).exists()
    assert archived(target, displaced) == displaced
    assert hosted.restore(target, snapshot, expected) == result


def test_refresh_refuses_a_hosted_copy_that_changed_after_its_expected_digest(diverged):
    source, target = diverged
    snapshot = hosted.capture(source, SLUG)
    stale = digest(hosted.capture(target, SLUG))
    media(target, b"Uploaded after the hosted digest was read.")
    before = hosted.capture(target, SLUG)
    with pytest.raises(ValueError, match="^hosted ledger changed since its expected digest$"):
        hosted.restore(target, snapshot, (digest(snapshot), stale))
    assert hosted.capture(target, SLUG) == before
    assert not (target.directory / f"{SLUG}.displaced").exists()


def test_refresh_refuses_a_snapshot_other_than_the_expected_source(diverged):
    source, target = diverged
    snapshot = hosted.capture(source, SLUG)
    before = hosted.capture(target, SLUG)
    with pytest.raises(ValueError, match="^snapshot differs from its expected source digest$"):
        hosted.restore(target, snapshot, ("0" * 64, digest(before)))
    assert hosted.capture(target, SLUG) == before


def test_refresh_never_creates_a_ledger_the_hosted_store_lacks(diverged, tmp_path):
    source, _ = diverged
    empty = SQLiteLedgerRepository(tmp_path / "empty" / DATABASE, ledger_core)
    empty.import_document("other-ledger", document())
    snapshot = hosted.capture(source, SLUG)
    with pytest.raises(ValueError, match="^no hosted ledger to refresh$"):
        hosted.restore(empty, snapshot, (digest(snapshot), "1" * 64))
    assert not empty.exists(SLUG)


def test_unguarded_import_still_refuses_a_diverged_hosted_copy(diverged):
    source, target = diverged
    before = hosted.capture(target, SLUG)
    with pytest.raises(ValueError, match="^hosted ledger conflict$"):
        hosted.restore(target, hosted.capture(source, SLUG))
    assert hosted.capture(target, SLUG) == before


def test_interrupted_refresh_keeps_the_hosted_copy_and_its_media(diverged, monkeypatch):
    source, target = diverged
    snapshot = hosted.capture(source, SLUG)
    before = hosted.capture(target, SLUG)
    monkeypatch.setattr(hosted, "write_files", lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        hosted.restore(target, snapshot, (digest(snapshot), digest(before)))
    assert hosted.capture(target, SLUG) == before
    assert archived(target, before) == before


def request(snapshot, credential, headers):
    encoded = gzip.compress(json.dumps(snapshot).encode())
    return SimpleNamespace(
        path=f"/api/v1/migrations/{SLUG}",
        command="POST",
        headers={
            "Host": "hosted.example",
            "X-Ledger-Migration": credential,
            "Content-Type": "application/gzip",
            "Content-Length": str(len(encoded)),
            **headers,
        },
        rfile=io.BytesIO(encoded),
    )


def test_migration_api_refreshes_only_with_both_wellformed_digests(diverged, monkeypatch):
    source, target = diverged
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    credential = hosted.token(secret, SLUG, 1100)
    snapshot = hosted.capture(source, SLUG)
    before = hosted.capture(target, SLUG)
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    guarded = {"X-Ledger-Source": digest(snapshot), "X-Ledger-Replaces": digest(before)}
    for headers in (
        {},
        {"X-Ledger-Source": digest(snapshot)},
        {"X-Ledger-Replaces": digest(before)},
        {**guarded, "X-Ledger-Replaces": digest(before).upper()},
        {**guarded, "X-Ledger-Source": digest(snapshot) + "0"},
    ):
        with pytest.raises(APIError) as failure:
            routes.dispatch(request(snapshot, credential, headers), server)
        assert failure.value.status == 400
        assert hosted.capture(target, SLUG) == before
    assert routes.dispatch(request(snapshot, credential, guarded), server)["data"] == hosted.manifest(snapshot)
    assert hosted.capture(target, SLUG) == snapshot


def test_transfer_names_both_digests_only_for_a_refresh(diverged, monkeypatch):
    source, _ = diverged
    snapshot = hosted.capture(source, SLUG)
    expected = hosted.manifest(snapshot)
    calls = []

    def opened(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps({"data": expected}).encode())

    monkeypatch.setattr(hosted.urllib.request, "build_opener", lambda handler: SimpleNamespace(open=opened))
    replaced = "b" * 64
    result = hosted.transfer("https://hosted.example", snapshot, secrets.token_hex(32), replaces=replaced)
    assert result == {"source": expected, "hosted": expected, "match": True, "replaced": replaced}
    assert calls[0].get_method() == "POST"
    assert calls[0].get_header("X-ledger-source") == expected["digest"]
    assert calls[0].get_header("X-ledger-replaces") == replaced
    calls.clear()
    assert hosted.transfer("https://hosted.example", snapshot, secrets.token_hex(32)) == {
        "source": expected,
        "hosted": expected,
        "match": True,
    }
    assert all(req.get_header("X-ledger-source") is None for req in calls)
    assert all(req.get_header("X-ledger-replaces") is None for req in calls)


def refresh_command(source, *extra):
    return ["migration", "--source", str(source.directory), "--url", "https://hosted.example", "--freeze", *extra]


def test_refresh_command_checks_the_frozen_source_before_transfer(diverged, monkeypatch, capsys):
    source, _ = diverged
    snapshot = hosted.capture(source, SLUG)
    monkeypatch.setenv("SWARM_SECRET", secrets.token_hex(32))
    sent = []

    def transfer(url, actual, supplied, replaces):
        sent.append((actual, replaces))
        return {"match": True, "replaced": replaces}

    monkeypatch.setattr(hosted, "transfer", transfer)
    flags = ("--slug", SLUG, "--replaces", "c" * 64)
    monkeypatch.setattr(sys, "argv", refresh_command(source, *flags, "--expect-source", "d" * 64))
    with pytest.raises(SystemExit) as failure:
        hosted.main()
    assert failure.value.code == 2
    assert capsys.readouterr().err.endswith("migration: error: frozen source differs from --expect-source\n")
    assert sent == []
    monkeypatch.setattr(sys, "argv", refresh_command(source, *flags, "--expect-source", digest(snapshot)))
    hosted.main()
    assert sent == [(snapshot, "c" * 64)]
    assert json.loads(capsys.readouterr().out) == {"match": True, "replaced": "c" * 64}


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--slug", SLUG, "--replaces", "c" * 64], "a hosted refresh names both --expect-source and --replaces"),
        (["--slug", SLUG, "--expect-source", "d" * 64], "a hosted refresh names both --expect-source and --replaces"),
        (
            ["--slug", SLUG, "--expect-source", "D" * 64, "--replaces", "c" * 64],
            "hosted refresh digests are 64 lowercase hexadecimal characters",
        ),
        (["--expect-source", "d" * 64, "--replaces", "c" * 64], "a hosted refresh names exactly one --slug"),
        (
            ["--slug", SLUG, "--slug", "other", "--expect-source", "d" * 64, "--replaces", "c" * 64],
            "a hosted refresh names exactly one --slug",
        ),
    ],
)
def test_refresh_command_errors_are_exact_and_precede_fencing(diverged, monkeypatch, capsys, extra, message):
    source, _ = diverged
    monkeypatch.setattr(sys, "argv", refresh_command(source, *extra))
    monkeypatch.setattr(hosted, "server_secret", lambda: pytest.fail("secret loaded before validation"))
    with pytest.raises(SystemExit) as failure:
        hosted.main()
    assert failure.value.code == 2
    assert capsys.readouterr().err.endswith(f"migration: error: {message}\n")
    assert not hosted.storage.fenced(source.directory, SLUG)


def test_refresh_command_requires_a_hosted_destination(diverged, monkeypatch, capsys):
    source, _ = diverged
    argv = ["migration", "--source", str(source.directory), "--slug", SLUG]
    monkeypatch.setattr(sys, "argv", [*argv, "--expect-source", "d" * 64, "--replaces", "c" * 64])
    with pytest.raises(SystemExit) as failure:
        hosted.main()
    assert failure.value.code == 2
    assert capsys.readouterr().err.endswith("migration: error: a hosted refresh requires a hosted destination\n")
