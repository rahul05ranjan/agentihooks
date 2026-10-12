import gc
import warnings

import pytest

from tests import test_swarm_v2_resource_sampling as original


def abandon_file(path):
    cycle = [path.open("w")]
    cycle.append(cycle)


@pytest.mark.parametrize("mode", ["isolated", "inherited", "drained", "inside"])
@pytest.mark.parametrize("iteration", range(5))
def test_warning_ownership(tmp_path, capfd, monkeypatch, mode, iteration):
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    path = tmp_path / f"{mode}-{iteration}.txt"
    try:
        if mode in ("inherited", "drained"):
            abandon_file(path)
        if mode == "drained":
            with warnings.catch_warnings(record=True) as before:
                warnings.simplefilter("always")
                gc.collect()
            assert any(str(path) in str(w.message) for w in before)
        if mode == "inside":
            measure = original.sampling.measure

            def leaking_measure(*args, **kwargs):
                result = measure(*args, **kwargs)
                abandon_file(path)
                return result

            monkeypatch.setattr(original.sampling, "measure", leaking_measure)
        if mode in ("inherited", "inside"):
            with pytest.raises(AssertionError) as failure:
                original.test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
            assert str(path) in str(failure.value)
            with capfd.disabled():
                print(f"GC-PROBE {mode} {iteration}: {failure.value}", flush=True)
            if mode == "inherited":
                original.test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
                with capfd.disabled():
                    print(f"GC-PROBE retry {iteration}: PASS", flush=True)
        else:
            original.test_measure_reports_the_whole_tree_of_a_live_workload(tmp_path, capfd)
            with capfd.disabled():
                print(f"GC-PROBE {mode} {iteration}: PASS", flush=True)
    finally:
        if enabled:
            gc.enable()
