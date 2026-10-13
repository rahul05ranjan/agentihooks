import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = """import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ['STATE'])
with (state / 'calls').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
def wait(name):
    deadline = time.monotonic() + 3
    while not (state / name).exists():
        if time.monotonic() >= deadline:
            sys.exit(41)
        time.sleep(0.01)
if args[0] == 'build':
    if '--no-cache-filter' in args:
        wait('scan-started')
        wait('profile-started')
        (state / 'rebuild').touch()
    lock = json.loads((Path(args[-1]) / 'docker/swarm-node/versions.lock').read_text())
    tool = lock['tools']['herdr']
    if 'sha256' not in tool:
        print('invalid artifact lock: herdr')
        sys.exit(1)
    if lock['architectures'] == ['arm64']:
        print('unsupported architecture')
        sys.exit(1)
    if tool['sha256'] == '0' * 64:
        print('artifact checksum mismatch: herdr')
        sys.exit(1)
elif args[:2] == ['image', 'inspect']:
    sys.exit(1)
elif args[0] == 'run':
    if '/opt/probe/profile-probe.sh' in args:
        if args[-1] == 'positive':
            (state / 'profile-started').touch()
            wait('rebuild')
        if os.environ['FAILURE'] == 'profile':
            sys.exit(18)
        print('{}')
    else:
        print(json.dumps({'manifest': {'source_revision': 'checked-revision',
              'declared': {'lock_sha256': 'locked'}},
              'worker_image_build_validation_failures': 0}))
"""
SCAN = """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == agentihooks-worker-proof:* ]]; then
    touch "$STATE/scan-started"
    for ((attempt=0; attempt<300; attempt++)); do
        if [[ -e "$STATE/rebuild" ]]; then break; fi
        sleep 0.01
    done
    [[ -e "$STATE/rebuild" ]] || exit 41
    if [[ "$FAILURE" == scan ]]; then exit 17; fi
fi
touch "$STATE/scan-${1%%:*}-finished"
"""
PROFILE = """import os, sys
from pathlib import Path
state = Path(os.environ['STATE'])
(state / 'profile-evidence').touch()
if os.environ['FAILURE'] == 'profile-evidence':
    sys.exit(19)
"""


def run_proof(tmp_path, failure, proof="all"):
    state = tmp_path / "state"
    state.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    archive = tmp_path / "source.tar"
    scan = (
        SCAN
        if proof == "all"
        else (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'touch "$STATE/scan-agentihooks-worker-proof-finished"\n'
            'if [[ "$FAILURE" == scan ]]; then exit 17; fi\n'
        )
    )
    files = {
        "docker/swarm-node/versions.lock": json.dumps(
            {"tools": {"herdr": {"sha256": "1" * 64}}, "architectures": ["amd64"]}
        ),
        "docker/swarm-node/scan.sh": scan,
        "docker/swarm-node/Dockerfile": "FROM scratch\n",
        "docker/swarm-node/fixtures/profiles/profile.json": "{}",
        "tests/swarm_node/profile_evidence.py": PROFILE,
        "tests/contracts/swarm_v2/fixtures/checkpoint/current.json": "{}",
    }
    with tarfile.open(archive, "w") as bundle:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            payload = content.encode()
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    scripts = {
        "docker": f"#!{sys.executable}\n" + DOCKER,
        "git": "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "$*" in\n'
        '    "rev-parse --show-toplevel") printf "%s\\n" "$REPO" ;;\n'
        '    "rev-parse HEAD") echo checked-revision ;;\n'
        '    "-C "*" archive HEAD") cat "$ARCHIVE" ;;\n'
        "    *) exit 1 ;;\nesac\n",
    }
    for name, content in scripts.items():
        path = commands / name
        path.write_text(content)
        path.chmod(0o755)
    output = tmp_path / "evidence"
    env = {key: value for key, value in os.environ.items() if key != "WORKER_BASE_CONTEXT"} | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "STATE": str(state),
        "REPO": str(tmp_path),
        "ARCHIVE": str(archive),
        "FAILURE": failure,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/smoke.sh"), str(output), proof],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
    return result, calls, state, output


def test_independent_worker_proofs_overlap_and_keep_every_check(tmp_path):
    result, calls, state, output = run_proof(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert "SV2-IMG-01 passed" in result.stdout
    assert (state / "profile-evidence").exists()
    assert (state / "scan-agentihooks-worker-proof-finished").exists()
    assert (state / "scan-agentihooks-worker-rebuild-finished").exists()
    builds = [call for call in calls if call[0] == "build"]
    assert len(builds) == 5
    assert "--no-cache-filter" in builds[-1]
    assert builds[-1][builds[-1].index("--no-cache-filter") + 1] == "worker"
    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == 12
    assert all("--read-only" in call and call[call.index("--network") + 1] == "none" for call in runs)
    profiles = [call[-1] for call in runs if "/opt/probe/profile-probe.sh" in call]
    assert profiles == ["positive", "positive", "rejection", "recovery", "rollback", "noexec"]
    assert json.loads((output / "result.json").read_text())["status"] == "passed"
    assert not list(output.glob("context-*"))
    assert calls[-1][:2] == ["image", "rm"]


@pytest.mark.parametrize("failure,status", [("scan", 17), ("profile", 18), ("profile-evidence", 19)])
def test_a_failed_background_worker_check_keeps_the_proof_red(tmp_path, failure, status):
    result, calls, state, output = run_proof(tmp_path, failure)
    assert result.returncode == status, result.stderr
    assert (state / "rebuild").exists()
    assert "SV2-IMG-01 passed" not in result.stdout
    assert not (output / "result.json").exists()
    assert not list(output.glob("context-*"))
    assert calls[-1][:2] == ["image", "rm"]


@pytest.mark.parametrize("proof,builds,runs", [("startup", 4, 5), ("rebuild", 1, 1), ("profiles", 1, 7)])
def test_worker_proof_modes_run_only_their_independent_checks(tmp_path, proof, builds, runs):
    result, calls, state, output = run_proof(tmp_path, "", proof)
    assert result.returncode == 0, result.stderr
    assert len([call for call in calls if call[0] == "build"]) == builds
    assert len([call for call in calls if call[0] == "run"]) == runs
    assert json.loads((output / "mode-result.json").read_text())["proof"] == proof
    assert not list(output.glob("context-*"))
    if proof == "rebuild":
        build = next(call for call in calls if call[0] == "build")
        assert build[build.index("--no-cache-filter") + 1] == "worker"
    if proof == "profiles":
        assert (state / "profile-evidence").exists()
    else:
        assert (state / "scan-agentihooks-worker-proof-finished").exists()


@pytest.mark.parametrize(
    "proof,failure,status",
    [
        ("startup", "scan", 17),
        ("rebuild", "scan", 17),
        ("profiles", "profile", 18),
        ("profiles", "profile-evidence", 19),
    ],
)
def test_a_failed_selected_worker_proof_never_reports_success(tmp_path, proof, failure, status):
    result, calls, state, output = run_proof(tmp_path, failure, proof)
    assert result.returncode == status, result.stderr
    assert not (output / "mode-result.json").exists()
    assert not list(output.glob("context-*"))


@pytest.mark.parametrize("change", ["none", "inventory", "revision", "missing", "profile"])
def test_parallel_worker_reports_require_equal_inventories_and_all_proofs(tmp_path, change):
    evidence = tmp_path / "evidence"
    report = {"manifest": {"source_revision": "checked-revision"}, "worker_image_build_validation_failures": 0}
    for proof in ("startup", "rebuild", "profiles"):
        folder = evidence / proof
        folder.mkdir(parents=True)
        (folder / "mode-result.json").write_text(
            json.dumps({"proof": proof, "tested_commit": "checked-revision", "status": "passed"})
        )
    for name in ("first", "second", "rollback"):
        (evidence / "startup" / f"{name}.json").write_text(json.dumps(report))
    (evidence / "profiles" / "profiles.json").write_text(json.dumps(report))
    for case in ("a", "b"):
        (evidence / "startup" / f"{case}-result.json").write_text(
            json.dumps({"package": "SV2-IMG-01", "tested_commit": "checked-revision"})
        )
    rebuild = report | ({"different": True} if change == "inventory" else {})
    (evidence / "rebuild" / "rebuild.json").write_text(json.dumps(rebuild))
    profiles = evidence / "profiles" / "SV2-IMG-02"
    profiles.mkdir()
    (profiles / "result.json").write_text(json.dumps({"status": "passed", "tested_commit": "checked-revision"}))
    if change == "revision":
        (evidence / "rebuild" / "mode-result.json").write_text(
            json.dumps({"proof": "rebuild", "tested_commit": "other", "status": "passed"})
        )
    if change == "missing":
        (evidence / "rebuild" / "rebuild.json").unlink()
    if change == "profile":
        (profiles / "result.json").write_text(json.dumps({"status": "failed", "tested_commit": "checked-revision"}))
    done = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/compare-smoke.sh"), str(evidence), "checked-revision"],
        capture_output=True,
        text=True,
    )
    assert (done.returncode == 0) == (change == "none"), done.stderr
    assert (evidence / "result.json").exists() == (change == "none")
