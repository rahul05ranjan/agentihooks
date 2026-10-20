from scripts.doctor import traces


def _record(costs):
    return {
        "merged": [task for task, _, _ in costs],
        "traces": [
            {
                "tags": [f"task:{task}"],
                "observations": [
                    {"type": "GENERATION", "turn": None, "tokens": tokens},
                    {"type": "SPAN", "turn": 1, "start": end - 10, "end": end},
                ],
            }
            for task, tokens, end in costs
        ],
    }


MERGED_FIRST = [("a", 400, 50), ("b", 500, 60), ("old", 800, 100)]
CHEAPER_LATER = [("c", 100, 200), ("d", 100, 210), ("e", 100, 220)]


def test_a_task_under_twice_the_median_at_its_merge_is_not_raised_by_cheaper_later_tasks():
    assert traces.task_cost(_record(MERGED_FIRST), traces.Limits()) == []
    assert traces.task_cost(_record(MERGED_FIRST + CHEAPER_LATER), traces.Limits()) == []


def test_a_task_over_twice_the_median_at_its_merge_is_still_raised():
    [found] = traces.task_cost(_record(MERGED_FIRST + CHEAPER_LATER + [("f", 2000, 300)]), traces.Limits())
    assert found.id == "task-cost/f"
    assert "uncached tokens 2000, median of the other merged tasks 250" in found.evidence


def test_a_task_merged_before_a_heavy_one_keeps_its_verdict():
    record = _record([("a", 400, 50), ("b", 500, 60), ("heavy", 5000, 100), ("old", 800, 70)])
    assert [f.subject for f in traces.task_cost(record, traces.Limits())] == ["heavy"]
