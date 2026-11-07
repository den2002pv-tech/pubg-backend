"""Small checks of benchmark statistics; performance runs stay outside pytest."""
from scripts.benchmark_scanner import summarize


def test_empty_and_singleton_samples():
    assert summarize([]) == {}
    assert summarize([7]) == {"n": 1, "median": 7, "p90": 7, "p95": 7, "max": 7}


def test_percentiles_interpolate_and_do_not_mutate_input():
    values = [100, 0, 50]
    assert summarize(values) == {"n": 3, "median": 50, "p90": 90, "p95": 95, "max": 100}
    assert values == [100, 0, 50]
