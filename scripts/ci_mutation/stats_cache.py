import hashlib
import json
import subprocess
from pathlib import Path

from scripts.ci_mutation.scope import is_test_file


def collect_cached_stats(
    root: Path, folder: Path, changes: dict, files: list[str], options: list[str], collect
) -> list[dict]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and not is_test_file(path) and (root / path).is_file()
    }
    context = json.dumps({"sources": sources, "changes": changes, "options": options}, sort_keys=True).encode()
    folder.mkdir(parents=True, exist_ok=True)
    keys = {path: hashlib.sha256(context + path.encode() + (root / path).read_bytes()).hexdigest() for path in files}
    reused, missing = [], []
    for path, key in keys.items():
        stored = folder / f"{key}.json"
        if stored.is_file():
            reused.append(json.loads(stored.read_text()))
        else:
            missing.append(path)
    print(f"Mutation stats cache: reused {len(reused)} test files; collect {len(missing)}", flush=True)
    fresh = collect(missing) if missing else []
    if any(result["status"] != 0 for result in fresh):
        raise SystemExit(1)
    for path in missing:
        durations = {
            test: seconds
            for result in fresh
            for test, seconds in result["durations"].items()
            if test.split("::", 1)[0] == path
        }
        tests = {
            function: sorted(
                {test for result in fresh for test in result["tests"].get(function, []) if test in durations}
            )
            for function in {function for result in fresh for function in result["tests"]}
        }
        (folder / f"{keys[path]}.json").write_text(
            json.dumps({"status": 0, "tests": tests, "durations": durations, "cpu": 0})
        )
    return reused + fresh
