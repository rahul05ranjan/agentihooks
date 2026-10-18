from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/docs-build.yml"


def test_documentation_pull_requests_build_the_pages_site_without_waiting_for_other_jobs():
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    pages = yaml.safe_load((_WORKFLOW.parent / "pages.yml").read_text())
    trigger = workflow[True]["pull_request"]
    assert trigger["branches"] == ["dev"]
    assert workflow[True]["push"]["branches"] == ["dev", "diffcheck/**"]
    assert set(pages[True]["push"]["paths"]) | {".github/workflows/docs-build.yml"} <= set(trigger["paths"])
    assert workflow[True]["push"]["paths"] == trigger["paths"]
    build = workflow["jobs"]["build"]
    assert not build.get("needs")
    jekyll = [step for step in build["steps"] if step.get("uses") == "actions/jekyll-build-pages@v1"]
    release = [step for step in pages["jobs"]["build"]["steps"] if step.get("uses") == "actions/jekyll-build-pages@v1"]
    assert len(jekyll) == len(release) == 1
    assert jekyll[0]["with"] == release[0]["with"] == {"source": "./", "destination": "./_site"}
    assert "if" not in jekyll[0]


def test_documentation_validation_cannot_configure_upload_or_deploy_pages():
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"build"}
    build = workflow["jobs"]["build"]
    assert build.get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert "environment" not in build
    assert [step["uses"] for step in build["steps"]] == ["actions/checkout@v4", "actions/jekyll-build-pages@v1"]
    assert build["steps"][0]["with"]["persist-credentials"] is False
    assert "secrets." not in _WORKFLOW.read_text()
