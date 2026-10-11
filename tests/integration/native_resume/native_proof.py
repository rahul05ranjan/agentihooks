"""Carry one real CLI conversation from a source home to a fresh home through a granted checkpoint store."""

import json
import sys
from pathlib import Path

from scripts.swarm_v2.checkpoints import model, native
from tests import sv2_ckp03_cases as cases


def newest(harness: str, home: Path, cwd: Path) -> str:
    if harness == "claude":
        found = sorted((home / "projects" / native.encoded(cwd)).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        return found[-1].stem
    found = sorted((home / "sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    files = {"rollout/x": found[-1].read_bytes()}
    return native.Codex().identity(files)[0]


def shown(result) -> dict:
    return json.loads(json.dumps(cases.outcome(result)))


def carry(harness: str, source: Path, source_cwd: Path, fresh: Path, fresh_cwd: Path, durable: Path, version: str):
    session = newest(harness, source, source_cwd)
    writer = native.Qualifier(model.Validator(cases.store(durable)))
    captured = writer.capture(harness, source, session, source_cwd, version, stopped=True)
    verdict = cases.commit(writer, captured, f"ckpt-live-{harness}", harness)
    reader = native.Qualifier(model.Validator(cases.store(durable, execution="e2", generation=3)))
    restored = reader.restore(f"ckpt-live-{harness}", fresh, fresh_cwd, version)
    material = captured.material() if isinstance(captured, native.NativeCapture) else b""
    return {
        "session_id": session,
        "capture": shown(captured),
        "committed": cases.shown(verdict),
        "restore": shown(restored),
        "material_names_auth_files": any(name.encode() in material for name in cases.AUTH_FILES[harness]),
        "auth_files_in_fresh_home": sorted(p.name for p in fresh.iterdir() if p.name in cases.AUTH_FILES[harness]),
        "native_resume_success_by_target": {"capture": writer.metrics(), "restore": reader.metrics()},
    }


def main(argv: list[str]) -> int:
    harness, *paths, version = argv
    source, source_cwd, fresh, fresh_cwd, durable = map(Path, paths)
    print(json.dumps(carry(harness, source, source_cwd, fresh, fresh_cwd, durable, version), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
