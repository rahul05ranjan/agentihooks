import os
from collections.abc import Iterable
from pathlib import Path


class KillMemory:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.mutant: str | None = None

    def _path(self, mutant: str) -> Path:
        return self.folder / mutant.partition("__mutmut_")[0]

    def order(self, mutant: str, tests: Iterable[str]) -> list[str]:
        path = self._path(mutant)
        killer = path.read_text() if path.is_file() else None
        return sorted(tests, key=lambda test: test != killer)

    def prepare(self, mutant: str | None, tests: Iterable[str]) -> Iterable[str]:
        self.mutant = mutant
        return tests if mutant is None else self.order(mutant, tests)

    def wrap(self, execute_pytest):
        def execute_remembering_kills(runner, params, **kwargs):
            if self.mutant is not None:
                kwargs["plugins"] = [*kwargs.get("plugins", []), self]
            return execute_pytest(runner, params, **kwargs)

        return execute_remembering_kills

    def pytest_runtest_logreport(self, report) -> None:
        if not report.failed or self.mutant is None:
            return
        path = self._path(self.mutant)
        self.mutant = None
        self.folder.mkdir(parents=True, exist_ok=True)
        # Children of one run write the same function concurrently; a rename never shows a partial name.
        partial = path.with_name(f"{path.name}.{os.getpid()}")
        partial.write_text(report.nodeid)
        partial.replace(path)
