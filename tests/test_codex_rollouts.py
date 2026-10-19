from pathlib import Path

from tests.test_profile_render import _write, world  # noqa: F401


def _rollout(sessions: Path, conversation: str) -> Path:
    return _write(sessions / "2026" / "10" / "18" / f"rollout-2026-10-18T16-13-16-{conversation}.jsonl", "{}\n")


def test_a_codex_rollout_survives_a_re_render_of_its_isolated_home(world):  # noqa: F811
    from scripts.profiles import render

    first = render.render_codex("rb-role", isolate=True)
    _rollout(first / "sessions", "01a14f5c")

    second = render.render_codex("rb-role", isolate=True, force=True)

    assert second.resolve() != first.resolve()
    assert list((second / "sessions").rglob("*01a14f5c.jsonl"))
    assert render.has_codex_rollout("rb-role", [], "01a14f5c")
    assert not render.has_codex_rollout("rb-role", [], "02b25e6d")
    assert not render.has_codex_rollout("rb-role", [], "")


def test_an_isolated_home_rendered_before_the_rollout_folder_is_adopted(world):  # noqa: F811
    from scripts.profiles import homes, render

    out = render.render_codex("rb-role", isolate=True)
    for item in homes.CODEX_ROLLOUTS:
        (out / item).unlink()
    _rollout(out / "sessions", "01a14f5c")
    _write(out / "archived_sessions" / "rollout-2026-10-17T10-00-00-0aa.jsonl", "{}\n")

    again = render.render_codex("rb-role", isolate=True)

    assert again is not None
    assert list((again / "sessions").rglob("*01a14f5c.jsonl"))
    assert list((again / "archived_sessions").rglob("*0aa.jsonl"))
    assert render.has_codex_rollout("rb-role", [], "01a14f5c")


def test_the_newer_rollout_wins_when_an_earlier_home_keeps_writing_it(world):  # noqa: F811
    import os

    from scripts.profiles import homes, render

    live = render.render_codex("rb-role", isolate=True)
    for item in homes.CODEX_ROLLOUTS:
        (live / item).unlink()
    older = _rollout(live / "sessions", "01a14f5c")
    render.render_codex("rb-role", isolate=True)
    stable = next(homes.rollouts(render.rendered_root(), "rb-role").rglob("*01a14f5c.jsonl"))
    older.write_text("{}\n{}\n")
    os.utime(older, (stable.stat().st_mtime + 5,) * 2)

    assert render.has_codex_rollout("rb-role", [], "01a14f5c")
    assert stable.read_text() == "{}\n{}\n"
    assert older.read_text() == "{}\n{}\n"
    assert not render.has_codex_rollout("rb-role", [], "a14f5c")
