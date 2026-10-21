from pathlib import Path

import pytest

from tests.ledger_load import gate, memory


@pytest.mark.parametrize("problems", [[], ["write latency breach"]])
def test_paired_load_uses_memory_storage_and_preserves_its_result_and_server_log(monkeypatch, tmp_path, problems):
    folders = []

    def run(folder):
        folders.append(folder)
        assert folder.is_relative_to(Path("/dev/shm"))
        (folder / "server.log").write_text("server measurement\n")
        return problems, 0.3

    def command():
        assert gate.run(tmp_path) == (problems, 0.3)
        return 17

    monkeypatch.setattr(gate, "stop_server", lambda process: None)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", command)
    assert memory.main() == 17
    assert (tmp_path / "server.log").read_text() == "server measurement\n"
    assert not folders[0].exists()


@pytest.mark.parametrize("log", ["server failed\n", None])
def test_paired_load_keeps_the_server_log_and_removes_storage_when_the_run_raises(monkeypatch, tmp_path, log):
    folders = []

    def run(folder):
        folders.append(folder)
        if log is not None:
            (folder / "server.log").write_text(log)
        raise RuntimeError("server exited")

    monkeypatch.setattr(gate, "stop_server", lambda process: None)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", lambda: gate.run(tmp_path))
    with pytest.raises(RuntimeError, match="server exited"):
        memory.main()
    if log is not None:
        assert (tmp_path / "server.log").read_text() == log
    assert not folders[0].exists()
