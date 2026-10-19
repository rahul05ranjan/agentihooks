import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from scripts.swarm_ledger.repository import LedgerRepository, postgres, sqlite
from scripts.swarm_ledger.storage_migration import hosted

DESTINATION = "shared database"
COUNTED = ("tasks", "events", "comments", "artifacts")


class DryRun(Exception):
    pass


def comments(value) -> int:
    if isinstance(value, list):
        return sum(comments(item) for item in value)
    if not isinstance(value, dict):
        return 0
    own = value.get("comments")
    return (len(own) if isinstance(own, list) else 0) + sum(comments(item) for item in value.values())


def counts(state: dict) -> dict:
    return {
        "tasks": len(state.get("tasks", [])),
        "events": len(state["_meta"].get("events", [])),
        "comments": comments({key: value for key, value in state.items() if key != "_meta"}),
        "artifacts": len(state.get("artifacts", [])),
    }


def slugs(repo: LedgerRepository) -> list:
    with repo.snapshot() as snapshot:
        return snapshot.slugs() if snapshot else []


def copy(source: LedgerRepository, target: LedgerRepository, slug: str, dry_run: bool, replace: bool = False) -> dict:
    with source.snapshot() as snapshot:
        state, registries, token = snapshot.export(slug), snapshot.registries(slug), snapshot.token(slug)
    try:
        with target.transaction() as transaction:
            exists = transaction.exists(slug)
            stale = not exists or transaction.export(slug) != state
            moved = transaction.registries(slug) != registries
            if exists and (stale or moved) and not replace:
                raise ValueError(f"ledger {slug} differs in the shared database; conversion refuses to replace it")
            if stale:
                stored, seeds = hosted.unseeded(state)
                transaction.insert(slug, stored, seeds, token or secrets.token_urlsafe(24))
            if moved:
                transaction.replace_registries(slug, registries)
            converted, kept = transaction.export(slug), transaction.registries(slug)
            if dry_run:
                raise DryRun
    except DryRun:
        pass
    source_counts, shared_counts = counts(state), counts(converted)
    match = source_counts == shared_counts and converted == state and kept == registries
    return {"slug": slug, "source": source_counts, "shared": shared_counts, "match": match}


def freeze(source: LedgerRepository) -> None:
    hosted.fence(source, "*", DESTINATION)


def convert(source: LedgerRepository, target: LedgerRepository, dry_run: bool = False) -> dict:
    copied = set()
    if not dry_run:
        for slug in slugs(source):
            copy(source, target, slug, False)
            copied.add(slug)
        freeze(source)
    ledgers = [copy(source, target, slug, dry_run, slug in copied) for slug in slugs(source)]
    totals = {side: {key: sum(row[side][key] for row in ledgers) for key in COUNTED} for side in ("source", "shared")}
    return {
        "ledgers": ledgers,
        "totals": {"ledgers": len(ledgers), **totals},
        "match": all(row["match"] for row in ledgers),
        "read_only": sqlite.fenced(source.directory, "*"),
        "dry_run": dry_run,
    }


def main() -> None:
    from hooks import config  # noqa: F401

    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_ledger.storage_migration.shared")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    address = postgres.url(os.environ)
    if not address:
        sys.exit(f"shared conversion: {postgres.URL} names no shared database")
    if not (args.source / sqlite.DATABASE).is_file():
        sys.exit(f"shared conversion: {args.source} holds no ledger database")
    from scripts.swarm_ledger import ledger_server

    source = sqlite.SQLiteLedgerRepository(args.source / sqlite.DATABASE, ledger_server.core)
    target = postgres.PostgresLedgerRepository(address, ledger_server.core)
    print(json.dumps(convert(source, target, args.dry_run)), flush=True)


if __name__ == "__main__":
    main()
