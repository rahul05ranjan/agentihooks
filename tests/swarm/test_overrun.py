from dataclasses import replace
from pathlib import Path

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, overrun
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

SLUG = "sw"
OWNER = "engineer@abc-0001"
SEAT = "eng-1@sw"
MINUTE = 60_000
START = 10_000_000
REPORT = START + overrun.REPORT_MINUTES * MINUTE
PARK = START + overrun.PARK_MINUTES * MINUTE
ENDED = PARK + overrun.GRACE_MINUTES * MINUTE


class Ledger:
    def __init__(self, task):
        self.rows = {"t1": {"id": "t1", "state": "claimed", "claimed_by": OWNER, "branch": "b1", **task}}
        self.comments, self.updates = [], []

    def state(self, slug):
        return {"tasks": list(self.rows.values()), "phases": [], "_meta": {"events": []}}

    def task(self, slug, task_id):
        assert slug == SLUG
        return self.rows.get(task_id)

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        assert slug == SLUG
        self.updates.append((task_id, fields, if_state))
        row = self.rows[task_id]
        if not if_state or row["state"] in if_state:
            row.update(fields)
        return dict(row)

    def comment(self, slug, task, text, by):
        assert slug == SLUG
        self.comments.append((task, text, by))


class Swarm:
    def __init__(self, task=None, live=True, retires=True, agent=None):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 1, 1))
        self.ledger = Ledger(task or {})
        self.retired, self.retires = [], retires
        self.live = {OWNER} if live else set()
        self.store.put_agent(SLUG, agent or AgentRecord(OWNER, "eng", "t1", seat=SEAT, started_at=START))
        self.store.redis.set(self.store.key(SLUG, "claim", "t1"), OWNER)

    def retire(self, agent):
        self.retired.append(agent.name)
        return self.retires

    def tick(self, at):
        watch = overrun.Watch(self.store, SLUG, self.ledger, frozenset(self.live), self.retire, at)
        return overrun.overrun_pass(watch, self.ledger.state(SLUG))

    def master(self):
        return InboxStore(self.store.redis).inbox("master@sw")

    def agent_inbox(self):
        return InboxStore(self.store.redis).inbox(OWNER)

    def working(self):
        return [a.name for a in self.store.agents(SLUG) if a.state != "finished"]


def _progress(lines):
    folder = Path.home() / ".agentihooks" / "swarm" / SLUG / "tasks" / "t1"
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_the_limits_are_set_in_code():
    assert (overrun.REPORT_MINUTES, overrun.PARK_MINUTES, overrun.GRACE_MINUTES) == (90, 150, 15)


def test_an_agent_under_the_report_limit_is_left_alone():
    swarm = Swarm()
    assert swarm.tick(REPORT - MINUTE) == []
    assert swarm.master() == [] and swarm.agent_inbox() == []


def test_past_the_report_limit_the_master_hears_the_task_its_age_and_last_steps_once():
    _progress(["opened the issue", "pushed the red test", "fixed the push guard", "waiting on the queue"])
    swarm = Swarm()
    actions = swarm.tick(REPORT)
    [item] = swarm.master()
    assert "t1" in item.text and OWNER in item.text and " 90 minutes " in item.text
    assert "pushed the red test" in item.text and "waiting on the queue" in item.text
    assert "opened the issue" not in item.text
    assert actions == [f"overrun: told master@sw that {OWNER} has run 90 minutes on task t1"]
    assert swarm.agent_inbox() == []
    assert swarm.tick(REPORT + 10 * MINUTE) == []
    assert len(swarm.master()) == 1


def test_a_report_without_progress_lines_says_none_were_recorded():
    swarm = Swarm()
    swarm.tick(REPORT)
    [item] = swarm.master()
    assert overrun.NO_STEPS in item.text


def test_past_the_park_limit_the_agent_is_asked_once_to_push_and_hand_off():
    swarm = Swarm()
    swarm.tick(REPORT)
    actions = swarm.tick(PARK)
    [ask] = swarm.agent_inbox()
    assert " 150 minutes " in ask.text and "t1" in ask.text
    assert "agentihooks swarm sw publish" in ask.text and "agentihooks swarm sw handoff" in ask.text
    assert f" {overrun.GRACE_MINUTES} minutes" in ask.text
    assert actions == [f"overrun: asked {OWNER} to push and hand off task t1 after 150 minutes"]
    assert swarm.retired == [] and swarm.working() == [OWNER]
    assert swarm.tick(PARK + MINUTE) == []
    assert len(swarm.agent_inbox()) == 1


def test_an_agent_first_seen_past_the_park_limit_is_reported_and_asked_in_one_tick():
    swarm = Swarm()
    actions = swarm.tick(PARK)
    assert len(actions) == 2 and len(swarm.master()) == 1 and len(swarm.agent_inbox()) == 1


def test_an_agent_still_live_after_the_grace_is_ended_and_its_task_reopened():
    swarm = Swarm()
    swarm.tick(PARK)
    assert swarm.tick(ENDED - MINUTE) == [] and swarm.retired == []
    actions = swarm.tick(ENDED)
    assert swarm.retired == [OWNER]
    row = swarm.ledger.rows["t1"]
    assert (row["state"], row["claimed_by"], row["awaiting"], row["branch"]) == ("open", "", "", "b1")
    assert swarm.ledger.updates[-1][2] == overrun.HELD
    assert swarm.working() == []
    assert swarm.store.claimant(SLUG, "t1") is None
    assert swarm.store.take_restart(SLUG, "t1") == overrun.CAUSE
    [(task, comment, by)] = swarm.ledger.comments
    assert (task, by) == ("t1", overrun.SENDER)
    assert " 165 minutes " in comment and f" {overrun.GRACE_MINUTES} minutes " in comment
    ended = swarm.master()[-1]
    assert OWNER in ended.text and "t1" in ended.text and "reopened" in ended.text
    assert actions == [f"overrun: ended {OWNER} on task t1 after 165 minutes and reopened the task"]
    assert swarm.store.redis.hgetall(swarm.store.key(SLUG, overrun.KEY)) == {}


def test_an_agent_that_handed_off_inside_the_grace_is_never_ended():
    swarm = Swarm()
    swarm.tick(PARK)
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, replace(agent, state="finished"))
    assert swarm.tick(ENDED) == []
    assert swarm.retired == [] and swarm.ledger.rows["t1"]["claimed_by"] == OWNER


def test_a_failed_end_keeps_the_task_claimed_and_retries_next_tick():
    swarm = Swarm(retires=False)
    swarm.tick(PARK)
    assert swarm.tick(ENDED) == [f"overrun: could not end {OWNER} on task t1, retrying next tick"]
    assert swarm.ledger.rows["t1"]["claimed_by"] == OWNER and swarm.working() == [OWNER]
    assert swarm.ledger.comments == []
    swarm.retires = True
    swarm.tick(ENDED + MINUTE)
    assert swarm.retired == [OWNER, OWNER] and swarm.ledger.rows["t1"]["state"] == "open"


def test_a_task_that_moved_on_is_not_reopened():
    swarm = Swarm()
    swarm.tick(PARK)
    swarm.ledger.task = lambda slug, task_id: {**swarm.ledger.rows[task_id], "state": "done"}
    assert swarm.tick(ENDED) == []
    assert swarm.retired == [] and swarm.ledger.updates == []


def test_a_pull_request_task_counts_as_claimed():
    swarm = Swarm(task={"state": "pr"})
    swarm.tick(PARK)
    swarm.tick(ENDED)
    assert swarm.retired == [OWNER] and swarm.ledger.rows["t1"]["state"] == "open"


def test_outage_time_does_not_count_toward_the_age(monkeypatch):
    monkeypatch.setattr(overrun.outage, "paused_ms", lambda slug, since, now: 30 * MINUTE)
    swarm = Swarm()
    assert swarm.tick(REPORT + 29 * MINUTE) == []
    assert len(swarm.tick(REPORT + 30 * MINUTE)) == 1


def test_an_outage_holds_every_step(monkeypatch):
    monkeypatch.setattr(overrun.outage, "held", lambda store, slug, name: "ledger health probe failed")
    swarm = Swarm()
    assert swarm.tick(ENDED + 60 * MINUTE) == []
    assert swarm.master() == [] and swarm.retired == []


def test_a_master_an_agent_off_its_claim_or_a_dead_pane_is_skipped():
    for swarm in (
        Swarm(agent=AgentRecord(OWNER, MASTER, "t1", started_at=START)),
        Swarm(task={"claimed_by": "engineer@abc-0002"}),
        Swarm(task={"state": "open"}),
        Swarm(live=False),
        Swarm(agent=AgentRecord(OWNER, "eng", "t1", seat=SEAT)),
        Swarm(agent=AgentRecord(OWNER, "eng", "", seat=SEAT, started_at=START)),
    ):
        assert swarm.tick(ENDED + 60 * MINUTE) == []
        assert swarm.master() == [] and swarm.retired == []


def test_a_new_life_on_the_same_task_starts_its_own_clock():
    swarm = Swarm()
    swarm.tick(PARK)
    swarm.store.drop_agent(SLUG, OWNER)
    successor = "engineer@abc-0002"
    swarm.store.put_agent(SLUG, AgentRecord(successor, "eng", "t1", seat=SEAT, started_at=PARK))
    swarm.ledger.rows["t1"]["claimed_by"] = successor
    swarm.live = {successor}
    assert swarm.tick(ENDED) == []
    assert swarm.store.redis.hgetall(swarm.store.key(SLUG, overrun.KEY)) == {}


def test_a_planner_is_watched_like_an_engineer():
    swarm = Swarm(agent=AgentRecord(OWNER, "plan", "t1", seat=SEAT, started_at=START))
    assert len(swarm.tick(PARK)) == 2
    swarm.tick(ENDED)
    assert swarm.retired == [OWNER]


def test_an_agent_released_through_ready_is_never_asked():
    swarm = Swarm(task={"state": "pr", "awaiting": "merge"})
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, replace(agent, state="finished"))
    assert swarm.tick(PARK) == [] and swarm.tick(ENDED) == []


def test_a_refused_reopen_leaves_the_claim_and_the_agent_record():
    swarm = Swarm()
    swarm.tick(PARK)
    swarm.ledger.update_task = lambda slug, task_id, fields, by="swarm", if_state=(): dict(swarm.ledger.rows[task_id])
    assert swarm.tick(ENDED) == [f"overrun: ended {OWNER} but could not reopen task t1, the stall watch reopens it"]
    assert swarm.store.claimant(SLUG, "t1") == OWNER and swarm.working() == [OWNER]
    assert swarm.store.take_restart(SLUG, "t1") == "" and swarm.ledger.comments == []


def test_a_pane_missing_for_one_tick_keeps_the_ask_and_its_grace():
    swarm = Swarm()
    swarm.tick(PARK)
    swarm.live = set()
    assert swarm.tick(PARK + MINUTE) == []
    swarm.live = {OWNER}
    assert swarm.tick(PARK + 2 * MINUTE) == [] and len(swarm.agent_inbox()) == 1
    assert len(swarm.tick(ENDED)) == 1 and swarm.retired == [OWNER]


def test_a_wait_on_the_merge_queue_holds_the_ask_and_the_end_but_not_the_report():
    swarm = Swarm(task={"state": "pr"})
    on = {"kind": "merge", "target": "https://github.com/o/r/pull/7"}
    idle.declare_wait(swarm.store.redis, SLUG, OWNER, ENDED + 60 * MINUTE, "queued", START, on=on)
    assert swarm.tick(PARK) == [f"overrun: told master@sw that {OWNER} has run 150 minutes on task t1"]
    assert swarm.tick(ENDED + MINUTE) == []
    assert swarm.agent_inbox() == [] and swarm.retired == []


def test_an_ended_merge_wait_or_a_checks_wait_holds_nothing():
    for on, until in (({"kind": "merge", "target": "u"}, PARK), ({"kind": "checks", "target": "u"}, PARK + MINUTE)):
        swarm = Swarm()
        idle.declare_wait(swarm.store.redis, SLUG, OWNER, until, "waiting", START, on=on)
        assert len(swarm.tick(PARK)) == 2 and len(swarm.agent_inbox()) == 1


def test_a_mark_left_from_another_task_starts_over():
    swarm = Swarm()
    swarm.tick(PARK)
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, replace(agent, task="t2"))
    swarm.ledger.rows["t2"] = {**swarm.ledger.rows["t1"], "id": "t2"}
    actions = swarm.tick(ENDED)
    assert swarm.retired == [] and len(actions) == 2
    assert len(swarm.agent_inbox()) == 2
