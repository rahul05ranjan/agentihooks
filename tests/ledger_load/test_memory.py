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

    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", command)
    assert memory.main() == 17
    assert (tmp_path / "server.log").read_text() == "server measurement\n"
    assert not folders[0].exists()


def test_paired_load_keeps_the_server_log_and_removes_storage_when_the_run_raises(monkeypatch, tmp_path):
    folders = []

    def run(folder):
        folders.append(folder)
        (folder / "server.log").write_text("server failed\n")
        raise RuntimeError("server exited")

    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", lambda: gate.run(tmp_path))
    with pytest.raises(RuntimeError, match="server exited"):
        memory.main()
    assert (tmp_path / "server.log").read_text() == "server failed\n"
    assert not folders[0].exists()
