import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Runs inside each tree so the base imports its own sources, never the head's.
_COLLECT = """
import json, sys, pytest

class Collected:
    nodeids = []

    def pytest_collection_finish(self, session):
        Collected.nodeids = [item.nodeid for item in session.items]

code = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", sys.argv[1]], plugins=[Collected()])
with open(sys.argv[2], "w") as out:
    json.dump(Collected.nodeids if code == 0 else None, out)
"""


def collect_all(trees: dict[str, Path], tests: str) -> dict[str, list[str] | None]:
    with tempfile.TemporaryDirectory() as scratch:
        outputs = {side: Path(scratch) / f"{side}.json" for side in trees}
        runs = {
            side: subprocess.Popen(
                [sys.executable, "-c", _COLLECT, tests, str(outputs[side])], cwd=tree, stdout=subprocess.DEVNULL
            )
            for side, tree in trees.items()
        }
        for run in runs.values():
            run.wait()
        return {side: json.loads(outputs[side].read_text()) if outputs[side].is_file() else None for side in trees}


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def top_level_names(tree: Path, tests: str) -> set[str]:
    skipped = (tree / tests).resolve()
    names = set()
    for root, dirs, files in os.walk(tree):
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".")
            and name not in {"node_modules", "__pycache__", "site-packages"}
            and (Path(root) / name).resolve() != skipped
        ]
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                module = ast.parse((Path(root) / name).read_bytes())
            except SyntaxError:
                continue
            names.update(node.name for node in module.body if isinstance(node, _DEFINITIONS))
    return names


def referenced_names(tree: Path, nodeid: str) -> set[str]:
    path, *scopes = nodeid.split("[", 1)[0].split("::")
    try:
        body = ast.parse((tree / path).read_bytes()).body
    except (OSError, SyntaxError):
        return set()
    node = None
    for scope in scopes:
        node = next((child for child in body if isinstance(child, _DEFINITIONS) and child.name == scope), None)
        if node is None:
            return set()
        body = node.body
    if isinstance(node, ast.ClassDef) or node is None:
        return set()
    names = set()
    for child in (descendant for statement in node.body for descendant in ast.walk(statement)):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.alias):
            names.add(child.name.rsplit(".", 1)[-1])
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head collects fewer tests than the base")
    parser.add_argument("--base", type=Path, required=True, help="checkout of the base revision")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--tests", default="tests/")
    args = parser.parse_args(argv)
    collected = collect_all({"base": args.base, "head": args.head}, args.tests)
    failed = [side for side, nodeids in collected.items() if not nodeids]
    for side in failed:
        print(f"::error::Collecting the {side} tests failed or found none, so the floor cannot be graded.")
    if failed:
        return 1
    base, head = collected["base"], collected["head"]
    lost = sorted(set(base) - set(head))
    admitted = {}
    if len(head) < len(base):
        deleted = top_level_names(args.base, args.tests) - top_level_names(args.head, args.tests)
        for nodeid in lost:
            if subjects := sorted(referenced_names(args.base, nodeid) & deleted):
                admitted[nodeid] = subjects
                print(f"{nodeid} is admitted: the head deletes {', '.join(subjects)}, which it referenced")
    floor = len(base) - len(admitted)
    print(f"head collects {len(head)} tests, base floor {floor}")
    if len(head) < floor:
        for nodeid in lost:
            if nodeid not in admitted:
                print(f"{nodeid} is in the base and not in the head")
        print(f"::error::The head collects {floor - len(head)} fewer tests than the base floor of {floor}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
