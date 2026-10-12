import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_read_back_accepts_the_operator_access_boundary(tmp_path):
    responses = {
        "orgs/the-cloud-clockwork": {"default_repository_permission": "read"},
        "orgs/the-cloud-clockwork/members?per_page=100": [{"login": "nestorcolt"}],
        "orgs/the-cloud-clockwork/actions/runner-groups/1": {
            "id": 1,
            "name": "Default",
            "allows_public_repositories": False,
        },
        "repos/the-cloud-clockwork/agentihooks/rulesets/24942335": {
            "name": "owner-and-apps-only-push",
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/gh-readonly-queue/**"]}},
            "rules": [{"type": "creation"}, {"type": "update"}],
            "bypass_actors": [
                {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"},
                {"actor_type": "Integration", "actor_id": 81054, "bypass_mode": "always"},
                {"actor_type": "Integration", "actor_id": 1144995, "bypass_mode": "always"},
                {"actor_type": "Integration", "actor_id": 1236702, "bypass_mode": "always"},
                {"actor_type": "Integration", "actor_id": 2988497, "bypass_mode": "always"},
            ],
        },
    }
    for repository in ("agentihooks", "agentibrain-kernel"):
        prefix = f"repos/the-cloud-clockwork/{repository}"
        responses[f"{prefix}/actions/permissions/fork-pr-contributor-approval"] = {
            "approval_policy": "all_external_contributors"
        }
        responses[f"{prefix}/actions/permissions/workflow"] = {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        responses[f"{prefix}/collaborators?per_page=100"] = [
            {"login": "nestorcolt", "permissions": {"push": True, "admin": True}}
        ]
        responses[f"{prefix}/teams?per_page=100"] = []
        responses[f"{prefix}/keys?per_page=100"] = []
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "assert sys.argv[1] == 'api'\n"
        "assert '--method' not in sys.argv and '-X' not in sys.argv\n"
        "data = json.load(open(os.environ['GH_ACCESS_FIXTURE']))\n"
        "value = data[sys.argv[2]]\n"
        "print(json.dumps(value))\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/github_access.sh"), "check"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GH_ACCESS_FIXTURE": str(fixture)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GitHub access controls verified" in result.stdout
