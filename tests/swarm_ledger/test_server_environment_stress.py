import json

import pytest

from tests.swarm_ledger.test_server_environment import ledger_server, served


@pytest.mark.parametrize("round_", range(300))
@pytest.mark.parametrize(
    ("host", "origin"),
    [("swarm.example.com", "https://swarm.example.com"), ("swarm.lan", "http://swarm.lan")],
)
def test_stress_a_listed_host_and_origin_are_served(tmp_path, ledger_port, round_, host, origin):
    with ledger_server(tmp_path, ledger_port) as port:
        status, body = served(port, "/api/v1/ledgers", host, origin, tmp_path / "server.log")
    assert status == 200
    assert json.loads(body)["data"] == []
