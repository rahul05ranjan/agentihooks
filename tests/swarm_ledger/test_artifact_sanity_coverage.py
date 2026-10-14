import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_browser_callbacks_record_only_executable_artifact_sanity_lines(tmp_path):
    root = Path(__file__).parents[2]
    script = textwrap.dedent("""
        import dis
        from pathlib import Path
        import types

        from coverage import Coverage
        from playwright.sync_api import sync_playwright

        root = Path.cwd()
        coverage = Coverage(config_file=str(root / '.github/coverage/coverage.ini'), data_file=None)
        coverage.start()
        from scripts.swarm_ledger import artifact_sanity as sanity

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium")
            try:
                files = sorted((root / 'tests/fixtures/artifacts').iterdir())
                assert sanity.run(browser, files) == {path.name: [] for path in files}
            finally:
                browser.close()
        coverage.stop()

        source = Path(sanity.__file__)
        pending = [compile(source.read_text(), str(source), 'exec')]
        executable = set()
        while pending:
            code = pending.pop()
            executable.update(line for _, line in dis.findlinestarts(code))
            pending.extend(value for value in code.co_consts if isinstance(value, types.CodeType))
        recorded = set(coverage.get_data().lines('scripts/swarm_ledger/artifact_sanity.py') or [])
        assert sanity.run.__code__.co_firstlineno + 1 in recorded, recorded
        assert not (impossible := recorded - executable), sorted(impossible)
    """)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_"))}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
