import inspect
import json
import re
import subprocess
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

from scripts.swarm_ledger.api.services import HttpPolicy, LedgerService, MediaService, Services, SwarmService

from scripts.swarm_ledger import api
from scripts.swarm_ledger.api import admin, architecture, hives, mutations, proofs, routes, routing
from scripts.swarm_ledger.storage_migration import hosted

SLUG = "fake-2026-01-01"
ROOT = Path(__file__).resolve().parents[2]


def fake_services(calls: list) -> Services:
    ledger = SimpleNamespace(
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(caller=lambda headers, admin, slug: "", controller=lambda credential: False),
        hub=None,
        home_summaries=lambda: [{"slug": SLUG, "title": "Fake"}],
        bin_summaries=lambda: [],
        bin_entries=lambda: {},
        bin_ledger=lambda slug: calls.append(("bin", slug)),
    )
    swarm = SimpleNamespace(
        doctor={"doctor_start": ["start"]},
        status=lambda slug: {"state": "running", "agents": [{"name": "eng"}]},
        control=lambda slug, argv, command="swarm": calls.append((command, slug, argv)) or ({}, ""),
        control_argv=lambda body: ["pause"],
    )
    repository = SimpleNamespace(token=lambda slug: "tok", exists=lambda slug: slug == SLUG)
    return Services(
        repository=repository,
        ledger=ledger,
        swarm=swarm,
        media=SimpleNamespace(),
        policy=HttpPolicy(allowed_hosts={"h"}, allowed_origins={"o"}, max_body=1 << 10),
    )


def request(command: str, path: str, payload: dict | None = None, origin: str | None = None) -> SimpleNamespace:
    headers = Message()
    headers["Host"] = "h"
    if origin:
        headers["Origin"] = origin
    raw = b""
    if payload is not None:
        raw = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
    sent = []
    return SimpleNamespace(
        command=command,
        path=path,
        headers=headers,
        rfile=BytesIO(raw),
        exists=lambda slug: slug == SLUG,
        send=lambda status, body, kind: sent.append((status, json.loads(body))),
        sent=sent,
    )


def served(services: Services, *args, **kwargs) -> tuple[int, dict]:
    handler = request(*args, **kwargs)
    api.handle(handler, services)
    return handler.sent[0]


def test_the_api_serves_ledger_reads_from_fake_services():
    status, body = served(fake_services([]), "GET", "/api/v1/ledgers")
    assert status == 200
    assert [(row["slug"], row["title"]) for row in body["data"]] == [(SLUG, "Fake")]


def test_the_api_serves_swarm_reads_from_the_swarm_service():
    status, body = served(fake_services([]), "GET", f"/api/v1/ledgers/{SLUG}/swarm/agents")
    assert status == 200
    assert [row["name"] for row in body["data"]] == ["eng"]


def test_the_api_runs_swarm_controls_through_the_swarm_service():
    calls = []
    status, body = served(fake_services(calls), "POST", f"/api/v1/ledgers/{SLUG}/swarm/actions", {"action": "pause"})
    assert (status, body) == (200, {"action": "pause", "accepted": True})
    assert calls == [("swarm", SLUG, ["pause"])]


def test_the_api_bins_through_the_ledger_service_and_stops_the_swarm():
    calls = []
    status, body = served(
        fake_services(calls), "POST", "/api/v1/bin/actions", {"action": "delete", "slug": SLUG}, origin="o"
    )
    assert (status, body) == (200, {"slug": SLUG, "action": "delete"})
    assert calls == [("bin", SLUG), ("swarm", SLUG, ["stop", "--now"])]


def test_the_api_refuses_hosts_outside_the_policy():
    handler = request("GET", "/api/v1/ledgers")
    handler.headers.replace_header("Host", "elsewhere")
    api.handle(handler, fake_services([]))
    assert handler.sent[0][0] == 403


def test_the_services_bundle_carries_interfaces_and_no_modules():
    services = fake_services([])
    assert not any(isinstance(getattr(services, field), ModuleType) for field in Services.__dataclass_fields__)
    assert {LedgerService, SwarmService, MediaService} <= set(Services.__annotations__.values())


def test_no_api_function_takes_the_server_module():
    modules = (admin, architecture, hives, mutations, proofs, routes, routing, hosted)
    taking = [
        f"{module.__name__}.{name}"
        for module in modules
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__ == module.__name__ and "server" in inspect.signature(function).parameters
    ]
    assert taking == []


def test_importing_the_api_loads_no_server_module():
    probe = (
        "import sys\n"
        "from scripts.swarm_ledger.api import admin, architecture, hives, mutations, proofs, routes, routing, services\n"
        "loaded = sorted(name for name in sys.modules if name.endswith('ledger_server'))\n"
        "assert loaded == [], loaded\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
