"""Grade a head load gate run against a base run made on the same runner."""

import argparse
import json
from pathlib import Path

MARGIN = 1.1
MEASURE = '{"seconds"'


def graded_measure(log: str) -> dict | None:
    """The measure the gate graded: its repeat when a lone write breach made it measure twice."""
    lines = [line for line in log.splitlines() if line.startswith(MEASURE)]
    return json.loads(lines[-1]) if lines else None


def regressions(base: dict | None, head: dict | None) -> list[str]:
    if base is None or head is None:
        return [f"the {'base' if base is None else 'head'} run printed no load measure"]
    problems = []
    if head["errors"] > base["errors"]:
        problems.append(f"head had {head['errors']} failed requests, base {base['errors']}")
    for key, label in (("write_p95_s", "write p95"), ("busiest_cores", "busiest CPU window")):
        if base[key] is None or head[key] is None:
            problems.append(f"{label} was not measured on the {'base' if base[key] is None else 'head'}")
        elif head[key] > base[key] * MARGIN:
            problems.append(f"head {label} {head[key]} exceeds base {base[key]} by more than {MARGIN}x")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_log", type=Path)
    parser.add_argument("head_log", type=Path)
    args = parser.parse_args(argv)
    base, head = (graded_measure(path.read_text()) for path in (args.base_log, args.head_log))
    print(json.dumps({"base": base, "head": head}, indent=2), flush=True)
    problems = regressions(base, head)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
