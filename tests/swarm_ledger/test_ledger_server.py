import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm.store import SwarmError  # noqa: E402
from scripts.swarm_ledger.storage import default as default_repository  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import chromium, rendered_home  # noqa: E402

storage = default_repository()

MINUTE = 60 * 1000
SWARM_STATE = server.swarm_state
SLUG = "rows-2026-01-03"


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", home_style()).group(1)


def home_style():
    return core.static_assets()["css/home.css"].read_text(encoding="utf-8").replace("\n", "")


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


def home(browser, view="home", now=None):
    return rendered_home(server, browser, view, now)


def one_row(browser, now, **fields):
    s = {"slug": "one", "title": "One", "overview": "o", "size": "swarm", "open": 0, "done": 0, "closed_at": None}
    with patch.object(server, "ledger_summaries", return_value=[{**s, **fields}]):
        return home(browser, now=now)


def make(slug, title="T", overview="O", size="swarm", phases=()):
    content = {"title": title, "overview": overview, "sources": [], "phases": list(phases), "questions": []}
    content["followups"] = []
    legacy_page.paths(slug, core)[0].write_text(
        legacy_page.render(new_ledger.build_doc(content, size), slug, 8765), encoding="utf-8"
    )


def task_op(n, **fields):
    op = {
        "op": "task_add",
        "id": f"add-{n}",
        "by": "row-engineer",
        "task": f"t{n}",
        "title": f"Task {n}",
        "lane": "eng",
    }
    return {**op, **fields}


def update(n, fields):
    return {"op": "task_update", "id": f"up-{n}", "by": "row-engineer", "item": f"tasks/t{n}", "fields": fields}


@pytest.fixture(scope="module")
def updated_at():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    make(SLUG, "Rows plan", "A long overview " * 20)
    ops = [task_op(n) for n in (1, 2, 3, 4)] + [update(3, {"state": "done"}), update(4, {"out_of_scope": True})]
    state, rejected = storage.apply_ops(SLUG, ops=ops)
    assert rejected == []
    return state["_meta"]["updated_at"]


@pytest.fixture(autouse=True)
def states():
    with storage.connect() as connection, connection:
        storage.save_registry(connection, "bin", {})
    with patch.object(server, "swarm_state", side_effect=lambda slug: "running" if slug == SLUG else None):
        yield


def row(page, slug):
    return re.search(rf'<li class="row"[^>]*>(?:(?!</li>).)*href="/{slug}"(?:(?!</li>).)*</li>', page, re.S).group(0)


def summary(slug):
    return {s["slug"]: s for s in storage.list_summaries()}[slug]


def test_home_spans_the_full_window_width():
    assert "max-width" not in rule("main")
    assert "max-width" not in rule("ul")


def test_each_row_carries_title_kind_counts_swarm_state_and_last_activity(browser, updated_at):
    found = row(home(browser, now=updated_at + 5 * MINUTE), SLUG)
    assert '<a class="title" href="/rows-2026-01-03" data-tip="Rows plan">Rows plan</a>' in found
    assert '<span class="kind">swarm</span>' in found
    assert '<span class="num open"><b>2</b> open</span><span class="num done"><b>1</b> done</span>' in found
    assert '<span class="state s-running">running</span>' in found
    assert re.search(r'<time class="when" datetime="[^"]+" data-tip="[^"]+">5m ago</time>', found)
    assert re.search(
        r'</a><span class="kind">swarm</span><span class="ov" data-tip="A long overview[^"]*">A long overview[^<]*</span>'
        r'<span class="num open">',
        found,
    )
    assert '<span class="acts"><button class="act del" type="button" data-act="delete"' in found
    assert 'data-slug="rows-2026-01-03" aria-label="Move Rows plan to the bin">' in found
    assert 'data-act="reopen"' not in found


def test_counts_skip_out_of_scope_tasks_and_activity_is_the_last_change(updated_at):
    found = summary(SLUG)
    assert (found["open"], found["done"], found["updated_at"]) == (2, 1, updated_at)


def test_a_ledger_without_tasks_counts_its_phases():
    make("phases-only", size="small", phases=[{"title": "a"}, {"title": "b"}])
    storage.apply_ops("phases-only", changes=[{"path": "phases/p1/done", "value": True}])
    found = summary("phases-only")
    assert (found["open"], found["done"], found["size"]) == (1, 1, "small")


def test_a_ledger_without_a_swarm_says_so_in_its_row(browser, updated_at):
    make("no-swarm")
    found = row(home(browser), "no-swarm")
    assert '<span class="state s-none">no swarm</span>' in found


def test_a_closed_ledger_shows_closed_and_a_reopen_button(browser, updated_at):
    make("closed-row")
    storage.apply_ops("closed-row", ops=[{"op": "close", "id": "c1", "by": "swarm"}])
    found = row(home(browser), "closed-row")
    assert '<span class="state s-closed">closed</span>' in found
    assert '<span class="acts"><button class="act reopen"' in found
    assert '>Reopen</button><button class="act del"' in found


def test_a_row_stays_on_one_line_and_truncates_the_overview():
    assert "grid-template-columns" in rule(".row")
    assert "white-space:nowrap" in rule(".row")
    for declaration in ("overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
        assert declaration in rule(".ov")


def test_buttons_and_the_floating_bin_entry_are_flat_at_rest():
    for selector in (".act", ".fab"):
        assert "background:transparent" in rule(selector)
        assert "border:0" in rule(selector)


def test_the_header_brands_home_without_a_ledger_count_and_names_each_column(browser, updated_at):
    make("header-a")
    make("header-b")
    page = home(browser)
    count = len(server.ledger_summaries())
    assert count > 1
    assert (
        '<header><a class="logo-link" href="/" aria-label="HOME"><span class="logo" aria-hidden="true"></span></a><span class="brand">agentihooks</span><h1>HOME</h1>'
        "</header>" in page
    )
    assert 'class="total"' not in page
    assert 'id="fold-all"' not in page
    head = re.search(
        r'<div class="row head">(.*?)</div>', re.sub(r'<span class="col-grip"[^>]*></span>', "", page)
    ).group(1)
    arrow = '<i aria-hidden="true">▼</i></button>'
    still = '<i aria-hidden="true">↕</i></button>'
    assert head == (
        f'<span>Ledger</span><button class="sort" type="button" data-sort="kind">Kind{still}'
        f'<span>Overview</span><button class="sort r" type="button" data-sort="open">Open{still}'
        f'<button class="sort r" type="button" data-sort="done">Done{still}'
        f'<button class="sort" type="button" data-sort="swarm">Swarm{still}'
        f'<button class="sort r" type="button" data-sort="at" data-dir="desc">Activity{arrow}<span></span>'
    )
    assert '<main class="home">' in page
    assert '</li><li class="row" data-slug=' in page


def test_the_bin_lists_days_left_and_a_restore_button(browser, updated_at):
    ledger_bin.delete(SLUG, now=updated_at)
    assert f'href="/{SLUG}"' not in home(browser)
    with patch.object(server.core, "now_ms", return_value=updated_at + 29 * 24 * 60 * MINUTE):
        page = home(browser, "bin")
    found = row(page, SLUG)
    date = server.time.strftime("%Y-%m-%d", server.time.localtime(updated_at / 1000))
    assert f'<span class="deleted">{date}</span><span class="left">1 day left</span>' in found
    assert f'<button class="act restore" type="button" data-act="restore" data-slug="{SLUG}"' in found
    assert "Restore</button>" in found
    assert (
        '<main class="bin"><header><a class="logo-link" href="/" aria-label="HOME"><span class="logo" aria-hidden="true"></span></a><span class="brand">agentihooks</span><h1>BIN</h1><span class="total" id="total">1 ledger</span></header>'
        in page
    )
    assert 'class="watermark"' not in page
    head = re.sub(r'<span class="col-grip"[^>]*></span>', "", page)
    assert '<span class="r">Deleted</span><span class="r">Left</span><span></span></div>' in head
    assert 'id="home-fab" href="/"' in page
    with patch.object(
        server, "bin_summaries", return_value=[{**summary(SLUG), "deleted_at": updated_at, "days_left": 30}]
    ):
        assert '<span class="left">30 days left</span>' in row(home(browser, "bin"), SLUG)


def test_the_bin_entry_carries_the_bin_count_and_an_empty_bin_says_so(browser, updated_at):
    assert '<span class="count">' not in home(browser)
    assert '<li class="empty">The bin is empty.</li>' in home(browser, "bin")
    ledger_bin.delete(SLUG)
    assert '<span class="count">1</span></a>' in home(browser)


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(0, "just now"), (0.9, "just now"), (1, "1m ago"), (59, "59m ago"), (60, "1h ago"), (1439, "23h ago")]
    + [(1440, "1d ago"), (3000, "2d ago")],
)
def test_last_activity_reads_in_the_largest_whole_unit(browser, minutes, text):
    assert f">{text}</time>" in one_row(browser, 1000 + int(minutes * MINUTE), updated_at=1000)


def test_activity_in_the_future_reads_just_now_and_unknown_when_missing(browser):
    assert ">just now</time>" in one_row(browser, 0, updated_at=5 * MINUTE)
    assert '<span class="when">unknown</span>' in one_row(browser, 0, updated_at=None)
    stamp = re.search(r'<time class="when"[^>]*>[^<]*</time>', one_row(browser, 3 * MINUTE, updated_at=MINUTE)).group(0)
    assert stamp.startswith('<time class="when" datetime="1970-01-01T00:01:00Z" data-tip="1970-01-01 ')
    assert stamp.endswith('">2m ago</time>')
    at = 1_791_290_000_000
    local = server.time.strftime("%Y-%m-%d %H:%M", server.time.localtime(at / 1000))
    assert f'data-tip="{local}">just now</time>' in one_row(browser, at, updated_at=at)


def test_the_swarm_state_comes_from_the_swarm_config():
    class Store:
        def __init__(self, error=None):
            self.error = error

        def config(self, slug):
            assert slug == "sw"
            if self.error:
                raise self.error
            return type("Config", (), {"state": "paused"})()

    with patch.object(server, "swarm_store", return_value=Store()):
        assert SWARM_STATE("sw") == "paused"
    with patch.object(server, "swarm_store", return_value=Store(SwarmError("no swarm sw"))):
        assert SWARM_STATE("sw") is None
    with (
        patch.object(server, "swarm_store", return_value=Store(ConnectionError("refused"))),
        patch.object(server.sys, "stderr") as err,
    ):
        assert SWARM_STATE("sw") is None
    err.write.assert_called_once_with("swarm state sw: refused\n")


def test_home_cells_escape_an_unknown_state(browser):
    with patch.object(server, "swarm_state", return_value="<b>"):
        page = one_row(browser, 0, updated_at=None)
    assert re.search(r'<span class="state s-(&lt;|<)b(&gt;|>)">&lt;b&gt;</span>', page)


class Swarms:
    def __init__(self, known=(), error=None):
        self.known, self.error = set(known), error

    def config(self, slug):
        if self.error:
            raise self.error
        if slug not in self.known:
            raise SwarmError(f"no swarm {slug}")
        return type("Config", (), {"state": "stopping"})()


def closed(slug):
    make(slug)
    storage.apply_ops(slug, ops=[{"op": "close", "id": "c1", "by": "swarm"}])
    return storage.apply_ops(slug)[0]["closed_at"]


def test_a_closed_ledger_without_a_swarm_goes_to_the_bin():
    closed_at = closed("orphan-closed")
    make("open-one")
    with patch.object(server, "swarm_store", return_value=Swarms()):
        server.bin_closed_without_swarm(now=closed_at + 5)
    assert ledger_bin.entries()["orphan-closed"] == closed_at + 5
    assert "open-one" not in ledger_bin.entries()


def test_a_closed_ledger_whose_swarm_is_still_stopping_stays_for_the_tick():
    closed("still-stopping")
    with patch.object(server, "swarm_store", return_value=Swarms({"still-stopping"})):
        server.bin_closed_without_swarm()
    assert "still-stopping" not in ledger_bin.entries()


def test_a_ledger_restored_then_closed_again_goes_back_to_the_bin():
    make("closed-twice")
    ledger_bin.delete("closed-twice", now=1)
    ledger_bin.restore("closed-twice", now=2)
    closed_at = closed("closed-twice")
    with patch.object(server, "swarm_store", return_value=Swarms()):
        server.bin_closed_without_swarm(now=closed_at + 1)
    assert ledger_bin.entries()["closed-twice"] == closed_at + 1


def test_an_unreachable_swarm_store_bins_nothing():
    closed("unreachable")
    with patch.object(server, "swarm_store", side_effect=SwarmError("Redis is unreachable")):
        server.bin_closed_without_swarm()
    with (
        patch.object(server, "swarm_store", return_value=Swarms(error=ConnectionError("refused"))),
        patch.object(server.sys, "stderr") as err,
    ):
        server.bin_closed_without_swarm()
    err.write.assert_called_once_with("bin closed ledgers: refused\n")
    assert "unreachable" not in ledger_bin.entries()


def test_the_home_shell_bins_nothing_and_the_watch_loop_bins_closed_ledgers_without_a_swarm():
    closed("served-closed")
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.headers = "/", {"Host": f"127.0.0.1:{server.PORT}"}
    sent = []
    handler.send = lambda code, body, ctype: sent.append((code, body))
    with patch.object(server, "swarm_store", return_value=Swarms()):
        handler.do_GET()
    assert sent[0][0] == 200
    assert "served-closed" not in ledger_bin.entries()

    def finish(interval):
        raise RuntimeError("watch ended")

    with (
        patch.object(server, "swarm_store", return_value=Swarms()),
        patch.object(server, "sample_streams"),
        patch.object(server, "scheduled_wait", finish),
        pytest.raises(RuntimeError, match="watch ended"),
    ):
        server.watch_ledgers()
    assert "served-closed" in ledger_bin.entries()


def test_draining_waits_for_the_requests_in_flight():
    import http.server
    import threading
    import urllib.request

    release = threading.Event()

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            release.wait(10)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    port = httpd.server_address[1]
    client = threading.Thread(
        target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).close(), daemon=True
    )
    try:
        assert httpd.drained(0.05) is True
        client.start()
        with httpd.idle:
            assert httpd.idle.wait_for(lambda: httpd.active == 1, 10)
        assert httpd.drained(0.05) is False
        release.set()
        assert httpd.drained(10) is True
        assert httpd.active == 0
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        client.join(10)


def test_an_inherited_socket_is_served_without_binding():
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        httpd = server.listening(held)
        try:
            assert httpd.socket is held
            assert httpd.request_queue_size == 128
        finally:
            httpd.socket = None


def test_a_fresh_server_binds_its_own_port(monkeypatch):
    import socket

    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    monkeypatch.setattr(server, "HOST", "127.0.0.1")
    monkeypatch.setattr(server, "PORT", port)
    httpd = server.listening()
    try:
        assert httpd.socket.getsockname() == ("127.0.0.1", port)
        assert httpd.request_queue_size == 128
    finally:
        httpd.server_close()


def test_warming_loads_every_open_ledger_and_survives_a_broken_one(monkeypatch):
    loaded = []

    def current(slug):
        loaded.append(slug)
        if slug == "broken":
            raise ValueError("bad rows")

    monkeypatch.setattr(server, "ledger_summaries", lambda: [{"slug": "broken"}, {"slug": "fine"}])
    monkeypatch.setattr(server.stored, "current", current)
    server.warm()
    assert loaded == ["broken", "fine"]


def test_a_hangup_asks_the_watch_loop_to_hand_off(monkeypatch):
    requested = server.threading.Event()
    monkeypatch.setattr(server, "RELOAD", requested)
    server.request_reload(1, None)
    assert requested.is_set()


def test_the_watch_loop_hands_off_on_request_and_then_stops(monkeypatch):
    requested = server.threading.Event()
    requested.set()
    seen = []
    monkeypatch.setattr(server, "RELOAD", requested)
    monkeypatch.setattr(server, "reloading", lambda: False)
    monkeypatch.setattr(server, "code_stamp", lambda: 7)

    def reload_if_changed(started, httpd, force):
        seen.append((started, httpd, force))

    monkeypatch.setattr(server, "reload_if_changed", reload_if_changed)
    assert server.watch_ledgers("httpd") is None
    assert seen == [(7, "httpd", True)]
    assert not requested.is_set()


def test_reload_signals_the_running_server_and_waits_for_its_successor(tmp_path, monkeypatch):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    sent, naps = [], []
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure"))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    def nap(seconds):
        naps.append(seconds)
        pidfile.write_text("42")

    monkeypatch.setattr(server.time, "sleep", nap)
    server.reload()
    assert sent == [(41, server.signal.SIGHUP), "ensure"]
    assert naps == [0.1]


def test_reload_starts_a_server_when_none_runs(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "server_process_alive", lambda: False)
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure"))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    server.reload()
    assert sent == ["ensure"]


def test_reload_fails_when_the_server_never_hands_off(tmp_path, monkeypatch):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit, match="pid 41 did not hand off within 2 s"):
        server.reload(wait=2.0)


def test_the_reload_flag_runs_reload(monkeypatch):
    ran = []
    monkeypatch.setattr(server, "reload", lambda: ran.append("reload"))
    monkeypatch.setattr(sys, "argv", ["ledger_server.py", "--reload"])
    server.main()
    assert ran == ["reload"]
