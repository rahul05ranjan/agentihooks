import ast
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from scripts.swarm_ledger import authority_context, ledger_authority


def test_trusted_authority_derives_worker_tokens_without_entering_the_environment(monkeypatch):
    from scripts.swarm import runtime
    from scripts.swarm_ledger import ledger

    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    with authority_context.bound(secret):
        assert authority_context.secret() == secret
        assert "SWARM_SECRET" not in os.environ
        expected = ledger_authority.agent_token(secret, "ledger", "worker")
        assert ledger.derived_token("ledger", "worker") == expected
        assert runtime._ledger_token("ledger", "worker") == {"AGENTIHOOKS_LEDGER_AGENT_TOKEN": expected}
    assert authority_context.secret() is None


def child_launches():
    root = Path(__file__).resolve().parents[2]
    result = []
    for folder in (root / "scripts", root / "hooks"):
        for path in folder.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update({item.asname or item.name: item.name for item in node.names})
                elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "asyncio", "os"):
                    aliases.update({item.asname or item.name: f"{node.module}.{item.name}" for item in node.names})
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                first, dot, rest = name.partition(".")
                name = aliases.get(first, first) + (dot + rest if dot else "")
                if name.startswith(
                    (
                        "subprocess.",
                        "asyncio.create_subprocess",
                        "os.spawn",
                        "os.posix_spawn",
                        "os.exec",
                        "os.fork",
                        "os.system",
                        "os.popen",
                    )
                ):
                    if name not in (
                        "subprocess.CompletedProcess",
                        "subprocess.TimeoutExpired",
                        "subprocess.CalledProcessError",
                        "subprocess.list2cmdline",
                        "subprocess.SubprocessError",
                    ):
                        result.append(f"{path.relative_to(root)}:{node.lineno}:{name}")
    return sorted(result)


def test_every_discovered_child_launch_inherits_no_server_authority(monkeypatch, capsys):
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    launches = child_launches()
    assert launches
    print(json.dumps({"child_launch_paths": launches}))
    code = "import os; from scripts.swarm_ledger import authority_context; print(bool(os.environ.get('SWARM_SECRET') or authority_context.secret()))"
    with authority_context.bound(secret):
        for _ in launches:
            assert "SWARM_SECRET" not in dict(os.environ)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "False"
        with subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ)
        ) as child:
            assert child.communicate()[0].strip() == "False"
        assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "False"
        assert authority_context.secret() == secret
    assert "child_launch_paths" in capsys.readouterr().out


def test_forked_children_cannot_read_private_authority(monkeypatch):
    secret = secrets.token_hex(32)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    read_end, write_end = os.pipe()
    with authority_context.bound(secret):
        child = os.fork()
        if child == 0:
            os.close(read_end)
            os.write(write_end, b"leaked" if authority_context.secret() else b"clean")
            os.close(write_end)
            os._exit(0)
        os.close(write_end)
        try:
            assert os.read(read_end, 16) == b"clean"
        finally:
            os.close(read_end)
            assert os.waitpid(child, 0)[1] == 0
        assert authority_context.secret() == secret


def test_nested_authority_scopes_restore_memory_without_restoring_exported_secrets(monkeypatch):
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    first, second = secrets.token_hex(32), secrets.token_hex(32)
    with authority_context.bound(first):
        with authority_context.bound(second):
            assert authority_context.secret() == second
        assert authority_context.secret() == first
    assert authority_context.secret() is None
