import argparse
import base64
import fcntl
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from pathlib import Path

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository import LedgerRepository

LIFETIME = 900
MAX_COMPRESSED = 64 << 20
MAX_EXPANDED = 256 << 20
FILE_ID = re.compile(r"[0-9a-f]{64}\.[a-z0-9]+\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TOKEN_MESSAGE = "ledger-import:{slug}:{expires}"


def encode(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def token(secret: str, slug: str, expires: int) -> str:
    message = TOKEN_MESSAGE.format(slug=slug, expires=expires)
    return f"{expires}.{hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}"


def authorize(secret: str, slug: str, credential: str, now: int) -> bool:
    try:
        expires = int(credential.split(".")[0])
    except (ValueError, AttributeError):
        return False
    return (
        bool(secret)
        and now < expires <= now + LIFETIME
        and hmac.compare_digest(credential.encode(), token(secret, slug, expires).encode())
    )


@contextmanager
def media_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ledger-media.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def media_mutation(directory: Path, slug: str) -> Iterator[bool]:
    with media_lock(directory):
        yield not storage.fenced(directory, slug)


def fence(repo: LedgerRepository, slug: str, destination: str) -> None:
    with repo.transaction() as transaction:
        transaction.prepare_fence(slug, destination)
        with media_lock(repo.directory):
            transaction.fence(slug, destination)


def capture(repo: LedgerRepository, slug: str) -> dict:
    with repo.snapshot() as snapshot:
        if snapshot is None:
            raise ValueError("no ledger database")
        state = snapshot.export(slug)
        registries = snapshot.registries(slug)
        folder = repo.directory / f"{slug}.media"
        files = {}
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("ledger media must contain regular files")
                files[path.name] = base64.b64encode(path.read_bytes()).decode()
        return {"version": 1, "slug": slug, "state": state, "registries": registries, "files": files}


def manifest(snapshot: dict) -> dict:
    state = snapshot["state"]
    meta = state["_meta"]
    return {
        "slug": snapshot["slug"],
        "revision": meta["rev"],
        "tasks": len(state.get("tasks", [])),
        "events": len(meta.get("events", [])),
        "revisions": len(meta.get("seeds", {})),
        "artifacts": len(state.get("artifacts", [])),
        "artifact_trash": len(state.get("artifact_trash", [])),
        "files": len(snapshot["files"]),
        "registries": len(snapshot["registries"]),
        "digest": hashlib.sha256(encode(snapshot)).hexdigest(),
    }


def file_bytes(files: dict) -> dict:
    result = {}
    for name, value in files.items():
        if not FILE_ID.fullmatch(name):
            raise ValueError("invalid content addressed file name")
        data = base64.b64decode(value, validate=True)
        if hashlib.sha256(data).hexdigest() != name.split(".")[0]:
            raise ValueError("file content does not match its name")
        result[name] = data
    return result


def write_files(folder: Path, files: dict) -> None:
    folder.mkdir(exist_ok=True)
    for name, data in files.items():
        path = folder / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError("hosted file conflict")
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(path)


def restore(repo: LedgerRepository, snapshot: dict, expected: tuple[str, str] | None = None) -> dict:
    slug, state = snapshot["slug"], snapshot["state"]
    if snapshot["version"] != 1 or not repo.domain.SLUG_RE.fullmatch(slug):
        raise ValueError("invalid migration identity")
    files = file_bytes(snapshot["files"])
    if any(path != slug for _, path, _ in snapshot["registries"]):
        raise ValueError("invalid migration registry")
    if expected and manifest(snapshot)["digest"] != expected[0]:
        raise ValueError("snapshot differs from its expected source digest")
    with repo.transaction() as transaction:
        displaced = displace(repo, snapshot, expected) if transaction.exists(slug) else None
        if displaced == snapshot:
            return manifest(snapshot)
        if expected and displaced is None:
            raise ValueError("no hosted ledger to refresh")
        write_files(repo.directory / f"{slug}.media", files)
        stored = json.loads(encode(state))
        seeds = stored["_meta"].get("seeds")
        if seeds is not None:
            stored["_meta"]["seeds"] = {}
        import secrets

        if displaced:
            transaction.drop_registries(slug)
        transaction.insert(slug, stored, seeds, transaction.token(slug) if displaced else secrets.token_urlsafe())
        transaction.add_registries(snapshot["registries"])
        if transaction.export(slug) != state:
            raise ValueError("imported history differs from source")
        folder = repo.directory / f"{slug}.media"
        displaced_only = displaced["files"].keys() - files.keys() if displaced else set()
        stale = {name: (folder / name).stat().st_mtime_ns for name in displaced_only}
    if stale:
        prune(repo, folder, stale)
    return manifest(capture(repo, slug))


def prune(repo: LedgerRepository, folder: Path, stale: dict) -> None:
    from scripts.swarm_ledger import request_metrics

    with request_metrics.locked(repo.domain.LOCK), media_lock(repo.directory):
        for name, written in stale.items():
            path = folder / name
            if path.exists() and path.stat().st_mtime_ns == written:
                path.unlink()


def displace(repo: LedgerRepository, snapshot: dict, expected: tuple[str, str] | None) -> dict:
    current = capture(repo, snapshot["slug"])
    if current == snapshot:
        return current
    if expected is None:
        raise ValueError("hosted ledger conflict")
    digest = manifest(current)["digest"]
    if digest != expected[1]:
        raise ValueError("hosted ledger changed since its expected digest")
    folder = repo.directory / f"{snapshot['slug']}.displaced"
    folder.mkdir(exist_ok=True)
    temporary = folder / f"{digest}.json.gz.part"
    with temporary.open("wb") as handle:
        handle.write(gzip.compress(encode(current)))
        os.fsync(handle.fileno())
    temporary.replace(folder / f"{digest}.json.gz")
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return current


def expectation(headers: Message | dict) -> tuple[str, str] | None:
    expected = (headers.get("X-Ledger-Source"), headers.get("X-Ledger-Replaces"))
    if expected == (None, None):
        return None
    if not all(isinstance(value, str) and DIGEST.fullmatch(value) for value in expected):
        raise ValueError("a refresh names both expected digests")
    return expected


def route(handler: object, server: object, parts: list) -> dict:
    if len(parts) != 2 or not server.core.SLUG_RE.fullmatch(parts[1]):
        raise APIError(404, "resource_missing", "No such migration")
    slug = parts[1]
    if not authorize(
        os.environ.get("SWARM_SECRET", ""), slug, handler.headers.get("X-Ledger-Migration", ""), int(time.time())
    ):
        raise APIError(403, "forbidden", "Missing or expired migration credential")
    if handler.command == "GET":
        return {"data": manifest(capture(server.repository, slug))}
    if handler.command != "POST":
        raise APIError(405, "method_not_allowed", "Use GET or POST for migration")
    if handler.headers.get("Content-Type") != "application/gzip":
        raise APIError(415, "content_type", "Migration requires application/gzip")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_COMPRESSED:
            raise ValueError("migration body exceeds compressed limit")
        with gzip.GzipFile(fileobj=io.BytesIO(handler.rfile.read(length))) as stream:
            data = stream.read(MAX_EXPANDED + 1)
        if len(data) > MAX_EXPANDED:
            raise ValueError("migration body exceeds expanded limit")
        snapshot = json.loads(data)
        if snapshot["slug"] != slug:
            raise ValueError("migration slug differs from route")
        result = restore(server.repository, snapshot, expectation(handler.headers))
    except (ValueError, KeyError, TypeError, EOFError, OSError) as error:
        raise APIError(400, "migration_invalid", "Migration refused: invalid data or existing conflict") from error
    return {"data": result}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transfer(url: str, snapshot: dict, secret: str, replaces: str | None = None) -> dict:
    headers = {
        "User-Agent": "agentihooks-swarm",
        "Content-Type": "application/gzip",
        "X-Ledger-Migration": token(secret, snapshot["slug"], int(time.time()) + LIFETIME),
        "CF-Access-Client-Id": os.environ.get("SWARM_ACCESS_CLIENT_ID") or os.environ.get("CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("SWARM_ACCESS_CLIENT_SECRET")
        or os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
    }
    from scripts.swarm_ledger.ledger_link import guard

    guard(url, headers)
    endpoint = url.rstrip("/") + "/api/v1/migrations/" + snapshot["slug"]
    opener = urllib.request.build_opener(NoRedirect)
    refresh = {"X-Ledger-Source": manifest(snapshot)["digest"], "X-Ledger-Replaces": replaces} if replaces else {}
    request = urllib.request.Request(endpoint, data=gzip.compress(encode(snapshot)), headers={**headers, **refresh})
    with opener.open(request, timeout=300) as response:
        result = json.load(response)["data"]
    with opener.open(urllib.request.Request(endpoint, headers=headers), timeout=300) as response:
        verified = json.load(response)["data"]
    if result != verified or verified != manifest(snapshot):
        raise ValueError("hosted comparison differs; do not cut over")
    report = {"source": manifest(snapshot), "hosted": verified, "match": True}
    return {**report, "replaced": replaces} if replaces else report


def server_secret() -> str:
    if secret := os.environ.get("SWARM_SECRET"):
        return secret
    base = os.environ.get("OPENBAO_URL", "").rstrip("/")
    credential = os.environ.get("OPENBAO_TOKEN")
    if not base or not credential:
        raise ValueError("trusted OpenBao credentials unavailable")
    request = urllib.request.Request(base + "/v1/secret/data/k8s/swarm", headers={"X-Vault-Token": credential})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        secret = json.load(response)["data"]["data"].get("SWARM_SECRET")
    if not secret:
        raise ValueError("OpenBao record k8s/swarm has no server secret")
    return secret


def main() -> None:
    from hooks import config  # noqa: F401

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--url")
    parser.add_argument("--slug", action="append")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--expect-source")
    parser.add_argument("--replaces")
    args = parser.parse_args()
    if args.url and not args.url.startswith("https://"):
        parser.error("hosted destination must use HTTPS")
    if args.freeze and not args.url:
        parser.error("freezing requires a hosted destination")
    if args.url and not args.freeze:
        parser.error("hosted transfers require source fencing with --freeze")
    refresh = refresh_request(parser, args)
    secret = server_secret() if args.url else None
    from scripts.swarm_ledger import ledger_server

    repo = storage.build(args.source / storage.DATABASE, ledger_server.core)
    if refresh and manifest(capture(repo, args.slug[0]))["digest"] != args.expect_source:
        parser.error("frozen source differs from --expect-source")
    with repo.snapshot() as snapshot:
        slugs = args.slug or snapshot.slugs()
    if args.freeze and not args.slug:
        fence(repo, "*", args.url)
        with repo.snapshot() as snapshot:
            slugs = snapshot.slugs()
    for slug in slugs:
        if args.freeze:
            fence(repo, slug, args.url)
        snapshot = capture(repo, slug)
        if refresh and manifest(snapshot)["digest"] != args.expect_source:
            parser.error("frozen source differs from --expect-source")
        result = transfer(args.url, snapshot, secret, **refresh) if args.url else manifest(snapshot)
        print(json.dumps(result), flush=True)


def refresh_request(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict:
    digests = (args.expect_source, args.replaces)
    if digests == (None, None):
        return {}
    if None in digests:
        parser.error("a hosted refresh names both --expect-source and --replaces")
    if not all(DIGEST.fullmatch(value) for value in digests):
        parser.error("hosted refresh digests are 64 lowercase hexadecimal characters")
    if not args.url:
        parser.error("a hosted refresh requires a hosted destination")
    if len(args.slug or []) != 1:
        parser.error("a hosted refresh names exactly one --slug")
    return {"replaces": args.replaces}


if __name__ == "__main__":
    main()
