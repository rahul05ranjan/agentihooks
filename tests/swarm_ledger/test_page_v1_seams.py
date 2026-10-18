import json
import re
from io import BytesIO
from unittest.mock import patch

import pytest

from scripts.swarm import freeze
from scripts.swarm_ledger.api.resources import revision
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm_ledger.ledger_page import MODULES, ledger_state, shell_html, show
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_api_v1 import request
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_ledger_authority import SLUG, send, server

pytestmark = pytest.mark.xdist_group("fakeredis")
authority_live = _authority_live
live = _live


def record(target, verb="freeze"):
    return {"id": f"{verb}-{target}", "verb": verb, "target": target, "by": "operator", "at": 1, "reason": ""}


def task(tid, phase, **fields):
    return {"id": tid, "title": tid, "phase": phase, "lane": "eng", "state": "open", **fields}


DOC = {
    "title": "Marks",
    "plans": [{"id": "p1", "title": "One"}, {"id": "p2", "title": "Two"}],
    "phases": [
        {"id": "ph1", "title": "First", "plan": "plans/p1"},
        {"id": "ph2", "title": "Second", "plan": "plans/p2"},
    ],
    "slices": [{"id": "s1", "phase": "phases/ph1", "anchor": "s1"}],
    "tasks": [
        task("t1", "ph1", slice="slices/s1"),
        task("t2", "ph2"),
        task("t3", "ph1", lane="ci"),
        task("t4", "ph2", rank="urgent"),
        task("t5", "ph2", merged_into="t1"),
    ],
}


def marks(freezes, fix_phase=""):
    doc = {**DOC, "freezes": freezes}
    found = freeze.marks(doc, hierarchy.project(doc)[0], fix_phase)
    return {key: set(found[key]) for key in ("frozen", "held")}


def test_a_frozen_plan_marks_every_node_under_it_and_the_tasks_folded_into_them():
    assert marks([record("plans/p2")]) == {
        "frozen": {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t4", "tasks/t5"},
        "held": set(),
    }


def test_a_focus_holds_what_lies_outside_it_but_never_urgent_or_fix_phase_tasks():
    assert marks([record("plans/p1", "focus")])["held"] == {"plans/p2", "phases/ph2", "tasks/t2"}
    assert marks([record("plans/p1", "focus")], fix_phase="ph2")["held"] == {"plans/p2", "phases/ph2"}


def test_a_task_focus_keeps_the_nodes_above_it_unheld():
    assert marks([record("tasks/t1", "focus")])["held"] == {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t3"}


def test_lane_records_mark_only_tasks():
    assert marks([record("lane:ci")])["frozen"] == {"tasks/t3"}
    assert marks([record("lane:ci", "focus")])["held"] == {"tasks/t1", "tasks/t2", "tasks/t5"}


def test_a_frozen_node_is_never_also_held():
    assert marks([record("plans/p2"), record("plans/p1", "focus")])["held"] == set()


def test_the_streamed_ledger_view_carries_the_marks_and_the_server_limits():
    from scripts.swarm_ledger import ledger_artifacts, ledger_core, ledger_server

    view = ledger_server.ledger_view(ledger_state({**DOC, "freezes": [record("plans/p2")]}))
    assert set(view["marks"]["frozen"]) == {"plans/p2", "phases/ph2", "tasks/t2", "tasks/t4", "tasks/t5"}
    assert view["marks"]["held"] == []
    cap, compact = [0, ledger_server.MAX_CAP], [ledger_server.MIN_COMPACT, ledger_server.MAX_COMPACT]
    assert view["_meta"]["limits"] == {
        "capacity": {"max_eng": cap, "max_ci": cap, "max_plan": cap, "masters_step": cap, "compact_limit": compact},
        "efforts": list(ledger_server.EFFORTS),
        "sync_cooldown_ms": ledger_core.SYNC_COOLDOWN_MS,
        "bin_days": ledger_artifacts.KEEP_DAYS,
    }


def test_page_saves_apply_through_version_one(live):
    op = {"op": "add", "id": "v1-page-save", "thread": "chat", "text": "Saved through version one"}
    status, reply = request(live, "PUT", "saves", {"ops": [op]})
    assert (status, reply["applied"]) == (200, ["v1-page-save"])
    assert any(row["id"] == "v1-page-save" for row in request(live, "GET", "chat?limit=100")[1]["data"])


def test_swarm_controls_and_the_model_list_answer_through_version_one(live):
    status_body = {"config": {"slug": SLUG, "state": "paused"}}
    with patch.object(server, "swarm_control", return_value=(status_body, None)) as control:
        assert request(live, "PUT", "swarm/controls", {"action": "pause"}) == (200, status_body)
    control.assert_called_once_with(SLUG, ["pause"], "swarm")
    catalog = {"claude": [{"model": "opus"}], "codex": []}
    with patch.object(server, "swarm_models", return_value=catalog):
        assert request(live, "GET", "swarm/models") == (200, {"data": catalog, "revision": revision(catalog)})


def test_uploaded_media_is_served_from_version_one(live):
    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    status, data, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/media",
        image.getvalue(),
        **{
            "X-Ledger-Token": live["admin"],
            "Content-Type": "application/octet-stream",
            "Origin": sorted(server.ALLOWED_ORIGINS)[0],
        },
    )
    assert status == 200
    media_id = json.loads(data)["id"]
    status, body, ctype = send(live, "GET", f"/api/v1/ledgers/{SLUG}/files/media/{media_id}")
    assert (status, body, ctype) == (200, image.getvalue(), "image/png")
    status, body, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}/files/media/missing.png")
    assert (status, json.loads(body)["error"]["code"]) == (404, "request_refused")


def test_the_version_one_layout_takes_the_page_layout_shape(live):
    layout = {
        "capacity-box": {"height": 312.5},
        "columns": {"swarm-agents": [120, 80.4]},
        "stats": ["elapsed", "started"],
    }
    status, data, _ = send(
        live,
        "PUT",
        "/api/v1/layout",
        json.dumps(layout).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    saved = {"capacity-box": {"height": 312}, "columns": {"swarm-agents": [120, 80]}, "stats": ["elapsed", "started"]}
    assert (status, json.loads(data)["data"]) == (200, saved)


def source(name):
    return (MODULES / f"{name}.js").read_text(encoding="utf-8")


def test_every_page_route_goes_through_version_one():
    routes = source("config") + source("api")
    assert not re.search(r"`/api/\$\{SLUG\}`\)|/api/swarm|/api/media|\"/api/layout\"|/media/\$\{SLUG\}", routes)
    assert "/artifacts/${SLUG}" not in routes
    for call in re.findall(r"fetch\(([^,)]+)", source("api")):
        assert "V1" in call or call == "artifactUrl(id", call


def test_the_page_copies_no_server_rule_or_limit():
    assert not re.search(r"SYNC_COOLDOWN_MS|ARTIFACT_KEEP_DAYS", source("config"))
    assert not re.search(r"\bancestors\b|rank === \"urgent\"", source("freezes"))
    controls = source("controls")
    assert '"xhigh"' not in controls
    assert not re.search(r"\b50\]|1000\]", controls)


browser = chromium_browser


def rendered(browser, ledger, swarm):
    tab = browser.new_page(viewport={"width": 1920, "height": 1080})
    tab.route("**/api/**", lambda route: route.abort())
    show(tab, shell_html(), ledger=ledger, swarm=swarm)
    return tab


SWARM = {
    "config": {"slug": SLUG, "state": "running", "max_eng": 3, "max_ci": 0, "max_plan": 1, "masters_step": 1},
    "compact_limit": 600,
    "agents": [],
    "tasks": {},
}


def test_the_page_renders_the_marks_the_server_sends_without_judging_freezes(browser):
    ledger = ledger_state({**DOC, "freezes": [], "marks": {"frozen": ["tasks/t2"], "held": ["tasks/t3"]}})
    tab = rendered(browser, ledger, SWARM)
    try:
        tab.click("#tasks-box > summary")
        tab.locator("#item-tasks-t3").wait_for()
        classes = tab.evaluate(
            "() => ['t1', 't2', 't3'].map((id) => document.getElementById(`item-tasks-${id}`).className)"
        )
        assert ["frozen" in c for c in classes] == [False, True, False]
        assert ["held" in c for c in classes] == [False, False, True]
    finally:
        tab.close()


def test_capacity_steppers_stop_at_the_bounds_the_server_publishes(browser):
    capacity = {"max_eng": [0, 3], "max_ci": [0, 2], "max_plan": [0, 2], "masters_step": [0, 2]}
    limits = {
        "capacity": {**capacity, "compact_limit": [100, 900]},
        "efforts": ["low", "high"],
        "sync_cooldown_ms": 0,
        "bin_days": 9,
    }
    swarm = {**SWARM, "config": {**SWARM["config"], "effort_min": "low", "effort_max": "high"}}
    tab = rendered(browser, {**DOC, "_meta": {"rev": 1, "limits": limits}}, swarm)
    try:
        off = tab.evaluate(
            """() => Object.fromEntries(['eng_up', 'eng_down', 'ci_down', 'effort_max_up', 'effort_min_up']
              .map((name) => [name, document.querySelector(`[data-swarm=${name}]`).disabled]))"""
        )
        assert off == {
            "eng_up": True,
            "eng_down": False,
            "ci_down": True,
            "effort_max_up": True,
            "effort_min_up": False,
        }
    finally:
        tab.close()
