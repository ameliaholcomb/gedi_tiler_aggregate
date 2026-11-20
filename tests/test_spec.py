"""Options to SQL: quality filters, aggregations, time windows."""

import pytest

from gtagg import spec

COLUMNS = {
    "l2a_quality_flag_rel3_l2a", "degrade_flag_l2a", "l2b_quality_flag_rel3_l2b",
    "l4a_quality_flag_rel3_l4a", "degrade_include_flag_l4a",
    "elev_highestreturn_outlier_flag_l4a", "l4c_quality_flag_rel3_l4c",
}


def test_variables():
    assert spec.parse_variables(["agbd_l4a", "rh98=rh_l2a[99]"]) == {
        "agbd_l4a": "agbd_l4a", "rh98": "rh_l2a[99]"}
    with pytest.raises(ValueError):
        spec.parse_variables(["rh 98=rh_l2a[99]"])


def test_quality_l2a_always():
    assert spec.quality_conditions(set(), COLUMNS) == [
        "l2a_quality_flag_rel3_l2a = 1", "degrade_flag_l2a = 0"]


def test_quality_l4a_alone_drops_degrade_flag():
    conds = spec.quality_conditions({"l4a"}, COLUMNS)
    assert "degrade_flag_l2a = 0" not in conds
    assert "degrade_include_flag_l4a = 1" in conds


def test_quality_l4a_with_l2a_keeps_degrade_flag():
    conds = spec.quality_conditions({"l4a", "l2a"}, COLUMNS)
    assert "degrade_flag_l2a = 0" in conds and "degrade_include_flag_l4a = 1" in conds


def test_quality_skips_missing_columns():
    # The database has no L4C outlier or degrade flags.
    conds = spec.quality_conditions({"l4c", "l2a"}, COLUMNS)
    assert "l4c_quality_flag_rel3_l4c = 1" in conds
    assert not any("_l4c" in c and "rel3" not in c for c in conds)


def test_products_from_expressions():
    assert spec.products(["rh_l2a[99]", "agbd_l4a / 2", "x"]) == {"l2a", "l4a"}


def test_agg_columns():
    variables = {"rh98": "rh98", "agbd": "agbd"}
    assert list(spec.agg_columns(variables, ["mean", "p95"])) == [
        "rh98_mean", "rh98_percentile_95", "agbd_mean", "agbd_percentile_95"]
    cols = spec.agg_columns(variables, {"rh98": ["median", "count"]})
    assert cols == {"rh98_median": "median(rh98)", "rh98_count": "count(rh98)"}
    with pytest.raises(ValueError):
        spec.agg_columns(variables, "mode")


def test_parse_aggregation():
    assert spec.parse_aggregation("mean") == "mean"
    assert spec.parse_aggregation("['mean','std']") == ["mean", "std"]
    assert spec.parse_aggregation("{'a': 'mean'}") == {"a": "mean"}


def test_time_windows():
    assert spec.time_windows("2020-01-01", "2021-07-01", 1, "years") == [
        ("2020-01-01-to-2021-01-01", "2020-01-01", "2021-01-01"),
        ("2021-01-01-to-2021-07-01", "2021-01-01", "2021-07-01"),
    ]
