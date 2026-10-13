import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
READER = "Read this attempt's jobs"
FAKE = r"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

endpoint = next(arg for arg in sys.argv if arg.startswith("repos/"))
if not urlsplit(endpoint).path.endswith("/jobs"):
    print(json.dumps({"run_started_at": "2026-10-13T00:00:00Z"}))
    sys.exit(0)
query = parse_qs(urlsplit(endpoint).query)
size = int(query.get("per_page", [30])[0])
page = int(query.get("page", [1])[0])
start = (page - 1) * size
with Path(os.environ["REQUESTS"]).open("a") as handle:
    handle.write(json.dumps([size, page]) + "\n")
fault = os.environ["FAULT"]
if fault == "client":
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
if fault == "server" or fault == "timeout":
    message = "gh: Bad Gateway (HTTP 502)" if fault == "server" else "gh: i/o timeout"
    print(message, file=sys.stderr)
    sys.exit(1)
if fault == "shrink" and (size > 5 or start >= 15 and size > 2):
    print("gh: Bad Gateway (HTTP 502)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"total_count": 23, "jobs": [{"id": i} for i in range(start, min(start + size, 23))]}))
"""


def _read(tmp_path, fault):
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE}")
    gh.chmod(0o755)
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["stage-budget"]["steps"] if step.get("name") == READER)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.defpath}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "7",
            "GITHUB_RUN_ATTEMPT": "2",
            "REQUESTS": str(tmp_path / "requests.jsonl"),
            "FAULT": fault,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    jobs = [json.loads(line) for line in (tmp_path / "jobs.jsonl").read_text().splitlines()]
    return result, requests, jobs


@pytest.mark.parametrize("fault", ["healthy", "shrink"])
def test_workflow_reader_lists_every_job_with_small_pages_after_server_errors(tmp_path, fault):
    result, requests, jobs = _read(tmp_path, fault)
    assert result.returncode == 0, result.stderr
    assert jobs == [{"id": i} for i in range(23)]
    assert max(size for size, _ in requests) <= 10
    if fault == "shrink":
        assert requests[:2] == [[10, 1], [5, 1]]
        assert [5, 4] in requests and [2, 8] in requests


def test_workflow_reader_fails_client_errors_without_retry(tmp_path):
    result, requests, jobs = _read(tmp_path, "client")
    assert result.returncode != 0
    assert "HTTP 404" in result.stderr
    assert len(requests) == 1
    assert jobs == []


@pytest.mark.parametrize("fault", ["server", "timeout"])
def test_workflow_reader_stops_after_bounded_retries(tmp_path, fault):
    result, requests, jobs = _read(tmp_path, fault)
    assert result.returncode != 0
    assert requests == [[10, 1], [5, 1], [2, 1], [1, 1]]
    assert jobs == []
