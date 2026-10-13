import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from scripts.ci_mutation.scope import imported_names, is_test_file, module_name


def test_inputs(
    root: Path, path: str, modules: dict[str, str], records: dict[str, tuple[str, set[str]]]
) -> dict[str, str]:
    pending = {path}
    inputs = {}
    while pending:
        current = pending.pop()
        if current not in records:
            text = (root / current).read_text()
            package = tuple(Path(current).with_suffix("").parts[:-1])
            imports = imported_names(text, package)
            names = {
                ".".join(name.split(".")[:index]) for name in imports for index in range(1, len(name.split(".")) + 1)
            }
            dependencies = (
                set(modules.values())
                if "importlib" in text or "__import__" in text
                else {modules[name] for name in names if name in modules}
            )
            records[current] = (hashlib.sha256(text.encode()).hexdigest(), dependencies)
        digest, dependencies = records[current]
        inputs[current] = digest
        pending.update(dependencies - inputs.keys())
    return inputs


def collect_cached_stats(
    root: Path,
    folder: Path,
    changes: dict,
    files: list[str],
    options: list[str],
    collect: Callable[[list[str]], list[dict]],
) -> list[dict]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and not is_test_file(path) and (root / path).is_file()
    }
    versions = {
        distribution.metadata["Name"]: distribution.version
        for distribution in metadata.distributions()
        if distribution.metadata["Name"].lower() != "agentihooks"
    }
    context = {
        "schema": 1,
        "sources": sources,
        "changes": changes,
        "options": options,
        "runtime": [sys.version, sys.platform, versions],
    }
    modules = {module_name(Path(path)): path for path in tracked if path and Path(path).suffix == ".py"}
    records = {}
    folder.mkdir(parents=True, exist_ok=True)
    keys = {
        path: hashlib.sha256(
            json.dumps({**context, "tests": test_inputs(root, path, modules, records)}, sort_keys=True).encode()
        ).hexdigest()
        for path in files
    }
    reused, missing = [], []
    for path, key in keys.items():
        stored = folder / f"{key}.json"
        if stored.is_file():
            result = json.loads(stored.read_text())
            if result["status"] == 0:
                reused.append(result)
            else:
                missing.append(path)
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
            json.dumps(
                {
                    "status": 0,
                    "tests": tests,
                    "durations": durations,
                    "cpu": sum(result["cpu"] for result in fresh) / len(missing),
                }
            )
        )
    return reused + fresh
