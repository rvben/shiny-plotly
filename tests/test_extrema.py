"""Exact indexed sampling and its lazy use for repeated broad views."""

import numpy as np
import pytest
from test_resample import reference_sample

from shiny_plotly._extrema import ExtremaIndex
from shiny_plotly._resample import resample_figure, sample


@pytest.mark.parametrize("kind", ["finite", "ties", "gaps", "infinities", "gap_block"])
def test_index_matches_independent_bucket_oracle_for_arbitrary_edges(kind):
    rng = np.random.default_rng(12)
    for n in (0, 1, 11, 64, 65, 103, 1003, 5003):
        y = rng.normal(size=n)
        if kind == "ties":
            y = np.resize(np.array([0.0, -0.0, 2.0, -2.0, 2.0, -2.0]), n)
        if n >= 1003:
            if kind == "gaps":
                y[::503] = np.nan
            elif kind == "infinities":
                y[13], y[-1] = np.inf, -np.inf
            elif kind == "gap_block" and n >= 5003:
                y[128:192] = np.nan
        y.flags.writeable = False
        before = y.copy()
        index = ExtremaIndex.build(y)
        assert index is not None
        for start, stop in ((0, n), (min(1, n), n), (n // 3, n * 2 // 3)):
            for budget in (10, 11, 102, 1000):
                for connect in (False, True):
                    expected = [start + i for i in reference_sample(y[start:stop], budget, connect)]
                    assert index.pick(start, stop, budget, connectgaps=connect).tolist() == expected
        np.testing.assert_array_equal(y, before)
        assert index.low.nbytes + index.high.nbytes + index.gaps.nbytes <= n * 0.375 + 16


def test_dense_gaps_decline_the_index():
    y = np.arange(1003, dtype=float)
    y[::3] = np.nan

    assert ExtremaIndex.build(y) is None


@pytest.mark.parametrize("dense", [False, True])
def test_only_repeated_broad_views_build_an_index_and_new_records_start_fresh(dense):
    n = 1_100_003
    x = np.arange(n, dtype=float)
    y = np.sin(x / 100)
    y[:: 3 if dense else 100_003] = np.nan
    figure = {"data": [{"x": x, "y": y}]}
    budget = 100
    _, record = resample_figure(figure, budget)
    series = record.series[0]
    assert series.extrema is None and series.broad_views == 0
    series.pick(budget, (100, 200))
    assert series.broad_views == 0
    for view in range(5):
        span = (float(view), float(n - 1 - view))
        kept = series.pick(budget, span)
        start, stop = max(view - 1, 0), min(n - view + 1, n)
        np.testing.assert_array_equal(kept, start + sample(y[start:stop], budget))
        assert series.index_attempted == (view >= 2)
        assert (series.extrema is not None) == (view >= 2 and not dense)
    # Narrow zooms retain the direct sampler even after an index exists.
    np.testing.assert_array_equal(series.pick(budget, (100, 200)), 99 + sample(y[99:202], budget))
    _, fresh = resample_figure(figure, budget)
    assert fresh.series[0].extrema is None
    assert not fresh.series[0].index_attempted
