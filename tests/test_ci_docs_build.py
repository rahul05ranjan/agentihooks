from pathlib import Path

import pytest
import yaml

from tests.test_ci_hosted_lanes import _evaluate

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/pages.yml"


def test_documentation_pull_requests_build_the_pages_site_without_waiting_for_other_jobs():
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    trigger = workflow[True]["pull_request"]
    assert trigger["branches"] == ["dev"]
    assert set(workflow[True]["push"]["paths"]) <= set(trigger["paths"])
    build = workflow["jobs"]["build"]
    assert not build.get("needs")
    assert "environment" not in build
    assert workflow["permissions"] == {"contents": "read"}
    assert build.get("permissions", workflow["permissions"]) == {"contents": "read"}
    jekyll = [step for step in build["steps"] if step.get("uses") == "actions/jekyll-build-pages@v1"]
    assert len(jekyll) == 1
    assert jekyll[0]["with"] == {"source": "./", "destination": "./_site"}
    assert "if" not in jekyll[0]


@pytest.mark.parametrize(
    ("event_name", "ref", "deploys"),
    [
        ("pull_request", "refs/pull/10/merge", False),
        ("pull_request", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/ci-proof", False),
        ("workflow_dispatch", "refs/heads/dev", False),
        ("workflow_dispatch", "refs/heads/main", True),
        ("push", "refs/heads/main", True),
    ],
)
def test_only_release_builds_configure_upload_or_deploy_pages(event_name, ref, deploys):
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    deploy = workflow["jobs"]["deploy"]
    assert _evaluate(deploy["if"], event_name=event_name, ref=ref) is deploys
    assert deploy["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    assert deploy["needs"] == "build"
    for step in workflow["jobs"]["build"]["steps"]:
        if step.get("uses", "").startswith(("actions/configure-pages@", "actions/upload-pages-artifact@")):
            assert _evaluate(step["if"], event_name=event_name, ref=ref) is deploys
