import statistics

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rightsize.stats import percentile


def test_linear_interpolation_matches_numpy_default():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 0) == 1
    assert percentile([1, 2, 3, 4], 100) == 4
    # numpy.percentile(range(1, 101), 95) == 95.05
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_order_does_not_matter():
    assert percentile([5, 1, 4, 2, 3], 75) == percentile([1, 2, 3, 4, 5], 75) == 4


def test_single_value_and_errors():
    assert percentile([7], 99) == 7
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1, 2], 101)


@given(st.lists(st.floats(0, 1e9), min_size=2, max_size=200))
def test_matches_statistics_inclusive_quantiles(values):
    # statistics.quantiles(method="inclusive") uses the same interpolation.
    q = statistics.quantiles(values, n=100, method="inclusive")
    assert percentile(values, 95) == pytest.approx(q[94], rel=1e-9, abs=1e-6)


@given(st.lists(st.floats(0, 1e6), min_size=1, max_size=100), st.floats(0, 100), st.floats(0, 100))
def test_monotone_in_q_and_bounded(values, a, b):
    lo, hi = sorted((a, b))
    assert min(values) <= percentile(values, lo) <= percentile(values, hi) + 1e-9
    assert percentile(values, hi) <= max(values) + 1e-9
