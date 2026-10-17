import json
import subprocess

from tests.swarm_ledger.ledger_page import page_source


def _function(page, name):
    return "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def _run(script):
    page = page_source()
    names = ("span", "modelText", "terminalView", "executionView", "agentRows", "remoteControlCell")
    stubs = """
const h = (tag, attrs, ...kids) => ({tag, attrs, kids});
const label = (text, tone) => ({label: text, tone});
"""
    js = stubs + "\n".join(_function(page, name) for name in names) + script
    return json.loads(subprocess.check_output(["node", "-e", js], text=True))


def test_only_the_master_row_carries_its_remote_control_line():
    rows = _run("""
const raw = {agents: [
  {name: 'm', lane: 'master', remote_control: 'Remote Control: on (master · sw)'},
  {name: 'e', lane: 'eng', remote_control: 'Remote Control: on (master · sw)'},
  {name: 'q', lane: 'master'}
]};
console.log(JSON.stringify(agentRows(raw, 0).map((r) => [r.name, r.remoteControl])));
""")
    assert rows == [["m", "Remote Control: on (master · sw)"], ["e", ""], ["q", ""]]


def test_the_cell_labels_the_state_and_shows_the_name_or_reason():
    cells = _run("""
console.log(JSON.stringify(['Remote Control: on (master · sw)', 'Remote Control: off (fallback auth)', 'odd'].map(remoteControlCell)));
""")
    assert [(c["attrs"]["data-tip"], c["kids"][0], c["kids"][1]["attrs"]["text"]) for c in cells] == [
        ("Remote Control: on (master · sw)", {"label": "remote control on", "tone": "on"}, "master · sw"),
        ("Remote Control: off (fallback auth)", {"label": "remote control off", "tone": "off"}, "fallback auth"),
        ("odd", {"label": "remote control off", "tone": "off"}, "odd"),
    ]
    assert "a.remoteControl ? remoteControlCell(a.remoteControl) : transportCell(a.transport)" in page_source()
