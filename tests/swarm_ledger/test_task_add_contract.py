import json
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger, ledger_server, new_ledger
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import legacy_page

SLUG = "task-add-contract"


@pytest.fixture
def cli(monkeypatch, ledger_port, capsys):
    server = ledger_server
    core = server.core
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    document = new_ledger.build_doc({"title": "Task contract", "phases": [{"title": "Proof"}]})
    core.paths(SLUG)[0].write_text(legacy_page.render(document, SLUG, ledger_port))
    core.sync(SLUG, ops=[])
    host = f"127.0.0.1:{ledger_port}"
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {host})
    monkeypatch.setattr(ledger, "BASE", f"http://{host}")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(server, "relay_to_inbox", lambda *args: None)
    monkeypatch.setattr(server, "doctor_phrase", lambda *args: None)

    def invoke(*options):
        monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "--as", "liaison", *options])
        code = 0
        try:
            ledger.main()
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr()
        return code, output.out, output.err

    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield invoke
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    ("option", "value", "field", "expected"),
    [
        ("--overlays", "tuner,trader", "overlays", ["tuner", "trader"]),
        ("--repositories", "/workspace/one,/workspace/two", "repositories", ["/workspace/one", "/workspace/two"]),
    ],
)
def test_task_add_client_fields_cross_the_real_server(cli, option, value, field, expected):
    code, output, error = cli("task", "add", "t1", "Build contract", option, value)
    assert code == 0, (output, error, code)
    assert json.loads(output) == {"task": "t1", "added": "Build contract"}
    code, output, error = cli("show")
    assert code == 0, error
    assert json.loads(output)["tasks"][0][field] == expected
    assert field in schemas.operation_schema("task_add")["properties"]
