import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SELECTORS = [
    ("coverage-ratchet", "Find the run that published the base baseline"),
    ("queue-baseline", "Find the run that measured the base tree"),
    ("durations", "Find the dev push run of the dispatched base"),
]


@pytest.fixture
def lookup(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['REQUESTS'], 'a') as stream: stream.write(json.dumps(args) + '\\n')\n"
        "data = json.loads(os.environ['SOURCE'])\n"
        "endpoint = args[1]\n"
        "path = urlsplit(endpoint).path\n"
        "query = parse_qs(urlsplit(endpoint).query)\n"
        "if '/compare/' in path: body = {'status': data['ancestry']}\n"
        "elif '/commits/' in path: body = {'sha': 'base', 'commit': {'tree': {'sha': 'tree'}}, 'parents': [{'sha': 'base'}]}\n"
        "elif path.endswith('/jobs'): body = {'jobs': data['jobs']}\n"
        "elif path.endswith('/artifacts'): body = {'artifacts': [{'name': name, 'expired': False} for name in data['artifacts']]}\n"
        "elif path.endswith('/runs'):\n"
        "    run = data['run']\n"
        "    keep = all(run.get(key) == value[0] for key, value in query.items() if key in ('head_sha', 'status', 'event'))\n"
        "    if 'status' in query and query['status'][0] == 'success': keep = run['conclusion'] == 'success'\n"
        "    if 'branch' in query: keep = keep and run['head_branch'] == query['branch'][0]\n"
        "    body = {'workflow_runs': [run] if keep else []}\n"
        "else: raise SystemExit('unexpected API endpoint: ' + endpoint)\n"
        "if '--jq' in args:\n"
        "    result = subprocess.run(['jq', '-r', args[args.index('--jq') + 1]], input=json.dumps(body), text=True, capture_output=True)\n"
        "    sys.stdout.write(result.stdout)\n"
        "    sys.stderr.write(result.stderr)\n"
        "    raise SystemExit(result.returncode)\n"
        "print(json.dumps(body))\n"
    )
    gh.chmod(0o755)

    def run(selector, event="push", fault=None):
        publisher, measure, publish = (
            ("coverage-baseline", "Compute the coverage baseline", "Publish the passed dev coverage baseline")
            if event == "push"
            else ("queue-baseline", "Record this run's coverage baseline", "Publish this run's coverage baseline")
        )
        jobs = [
            {"name": f"unit ({version}, {shard})", "conclusion": "success"}
            for version, count in (("3.11", 6), ("3.12", 12))
            for shard in range(1, count + 1)
        ]
        jobs.extend(
            [
                {
                    "name": publisher,
                    "conclusion": "success",
                    "steps": [{"name": name, "conclusion": "success"} for name in (measure, publish)],
                },
                {"name": "coverage-ratchet", "conclusion": "failure"},
            ]
        )
        source = {
            "run": {
                "id": 222,
                "event": event,
                "head_sha": "base",
                "head_branch": "dev" if event == "push" else "gh-readonly-queue/dev/pr-1-base",
                "status": "completed",
                "conclusion": "failure",
            },
            "ancestry": "ahead",
            "jobs": jobs,
            "artifacts": ["coverage-baseline", "durations-merged"],
        }
        if fault == "unit":
            jobs[0]["conclusion"] = "failure"
        elif fault == "missing-unit":
            jobs.pop(0)
        elif fault == "measure":
            jobs[-2]["steps"][0]["conclusion"] = "failure"
        elif fault == "publish":
            jobs[-2]["steps"][1]["conclusion"] = "failure"
        elif fault == "missing-publisher":
            jobs.pop(-2)
        elif fault == "outside-dev":
            source["ancestry"] = "diverged"
        elif fault == "event":
            source["run"]["event"] = "pull_request"
        elif fault == "branch":
            source["run"]["head_branch"] = "feature"
        elif fault == "head":
            source["run"]["head_sha"] = "other"
        elif fault == "artifact":
            source["artifacts"] = []
        output = tmp_path / "output"
        output.write_text("")
        requests = tmp_path / "requests"
        requests.write_text("")
        env = dict(
            os.environ,
            PATH=f"{tools}:{os.environ['PATH']}",
            SOURCE=json.dumps(source),
            REQUESTS=str(requests),
            GITHUB_OUTPUT=str(output),
            GITHUB_REPOSITORY="o/r",
            GITHUB_REF_NAME="feature",
            GITHUB_SHA="head",
            BASE="base",
            BASE_SHA="base",
            WAIT_SECONDS="0",
        )
        job, name = selector
        steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"][job]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == name)
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        return result, output.read_text(), requests.read_text()

    return run


@pytest.mark.parametrize("selector", _SELECTORS)
@pytest.mark.parametrize("event", ["push", "merge_group"])
def test_lookup_accepts_measured_publication_when_its_ratchet_failed(lookup, selector, event):
    if selector[0] == "durations" and event == "merge_group":
        return
    result, output, requests = lookup(selector, event=event)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "id=222\n" in output
    assert "/runs/222/jobs" in requests


@pytest.mark.parametrize("selector", _SELECTORS)
@pytest.mark.parametrize(
    "fault",
    [
        "unit",
        "missing-unit",
        "measure",
        "publish",
        "missing-publisher",
        "outside-dev",
        "event",
        "branch",
        "head",
        "artifact",
    ],
)
def test_lookup_rejects_sources_without_valid_dev_measurements(lookup, selector, fault):
    result, output, _ = lookup(selector, fault=fault)
    assert result.returncode != 0, result.stdout + result.stderr
    assert output == ""
