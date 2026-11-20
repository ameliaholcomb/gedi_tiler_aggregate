"""Turn GEDI-H3 style options into SQL over the tiled GEDI database.

Variables are columns of the database, or `name=SQL expression`, e.g.
`rh98=rh_l2a[99]` (SQL lists are 1-indexed, so rh_l2a[99] is RH98).
Filters (`--query`) are SQL boolean expressions, not pandas queries.
"""

import ast
import datetime
import json
import os
import re

from dateutil.relativedelta import relativedelta

# GEDI-H3's --quality conditions for GEDI release 3 (gedih3/config.py),
# with the tiled database's product-suffixed column names. Conditions on
# columns the database lacks are skipped, as GEDI-H3 does.
QUALITY = {
    "l1b": ["stale_return_flag_l1b = 0", "rx_clipflag_l1b = 0"],
    "l2a": ["l2a_quality_flag_rel3_l2a = 1", "degrade_flag_l2a = 0"],
    "l2b": ["l2b_quality_flag_rel3_l2b = 1"],
    "l4a": [
        "l4a_quality_flag_rel3_l4a = 1",
        "degrade_include_flag_l4a = 1",
        "elev_highestreturn_outlier_flag_l4a = 0",
    ],
    "l4c": [
        "l4c_quality_flag_rel3_l4c = 1",
        "degrade_include_flag_l4c = 1",
        "elev_highestreturn_outlier_flag_l4c = 0",
    ],
}
POWER_BEAMS = ("BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011")
TIME_UNITS = ("years", "months", "weeks", "days")


def parse_variables(items):
    """{output name: SQL expression} from `col` or `name=expr` items."""
    out = {}
    for item in items or []:
        name, sep, expr = item.partition("=")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Bad variable name {name!r} in {item!r}")
        out[name] = expr.strip() if sep else name
    return out


def products(exprs):
    """GEDI products whose columns the expressions use, from column suffixes."""
    found = set()
    for e in exprs:
        found |= {m.lower() for m in re.findall(r"_(l1b|l2a|l2b|l4a|l4c)\b", e, re.I)}
    return found


def quality_conditions(used_products, columns):
    """GEDI-H3's --quality: every selected product's conditions, plus L2A's
    always. With no L2 product selected, an L4 product's own
    degrade_include_flag replaces L2A's degrade_flag."""
    selected = set(used_products)
    conds = []
    for p in ["l2a"] + sorted(selected - {"l2a"}):
        conds += [c for c in QUALITY[p] if c.split()[0] in columns]
    if selected and not selected & {"l2a", "l2b"}:
        if any(c.startswith("degrade_include_flag_") for c in conds):
            conds = [c for c in conds if not c.startswith("degrade_flag_l2a")]
    return conds


def where_sql(spec, columns):
    """SQL conditions (ANDed) for a spec's shot filters."""
    conds = []
    if spec["quality"]:
        exprs = list(spec["variables"].values()) + [spec["query"] or ""]
        conds += quality_conditions(products(exprs), columns)
    if spec["beam_type"] == "power":
        conds.append(f"beam_name IN {POWER_BEAMS}")
    elif spec["beam_type"] == "coverage":
        conds.append(f"beam_name NOT IN {POWER_BEAMS}")
    if spec["time_start"]:
        conds.append(f"absolute_time >= TIMESTAMPTZ '{spec['time_start']} 00:00:00+00'")
    if spec["time_end"]:
        # As GEDI-H3: up to the first instant of the end date.
        conds.append(f"absolute_time <= TIMESTAMPTZ '{spec['time_end']} 00:00:00+00'")
    if spec["query"]:
        conds.append(f"({spec['query']})")
    return conds


# --- Aggregation functions --------------------------------------------------

_FUNCS = {
    "mean": "avg({})",
    "median": "median({})",
    "std": "stddev_samp({})",  # pandas' default, ddof=1
    "var": "var_samp({})",
    "min": "min({})",
    "max": "max({})",
    "sum": "sum({})",
    "count": "count({})",
    "nunique": "count(DISTINCT {})",
    "first": "first({})",
    "last": "last({})",
}


def agg_sql(func, expr):
    """(SQL, column suffix) of one aggregation function, as GEDI-H3 names
    them: `{var}_{func}`, and pNN as `{var}_percentile_NN`."""
    m = re.fullmatch(r"p(\d{1,2})", func)
    if m:
        q = int(m.group(1))
        return f"quantile_cont({expr}, {q / 100})", f"percentile_{q}"
    if func not in _FUNCS:
        raise ValueError(f"Unknown aggregation {func!r}: use {sorted(_FUNCS)} or pNN")
    return _FUNCS[func].format(expr), func


def parse_aggregation(text):
    """GEDI-H3's -a: a function, a list, a {variable: function(s)} dict,
    or a .json or text file (one function per line) holding one."""
    text = text.strip()
    if os.path.isfile(text):
        with open(text) as f:
            if text.endswith(".json"):
                return json.load(f)
            funcs = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
            return funcs if len(funcs) > 1 else funcs[0]
    if text[:1] in "[{":
        return ast.literal_eval(text)
    return text


def agg_columns(variables, agg):
    """{output column: SQL} of every aggregate."""
    if isinstance(agg, dict):
        missing = set(agg) - set(variables)
        if missing:
            raise ValueError(f"Aggregation names unknown variables {sorted(missing)}")
        per_var = agg
    else:
        per_var = {v: agg for v in variables}
    out = {}
    for var, funcs in per_var.items():
        for func in [funcs] if isinstance(funcs, str) else funcs:
            sql, suffix = agg_sql(func, variables[var])
            out[f"{var}_{suffix}"] = sql
    return out


# --- Time-series windows ----------------------------------------------------

def time_windows(start, end, interval, units):
    """GEDI-H3's time-series windows: [(label, t0, t1)], t1 exclusive."""
    if units not in TIME_UNITS:
        raise ValueError(f"Time units must be one of {TIME_UNITS}")
    t0 = datetime.date.fromisoformat(start)
    end = datetime.date.fromisoformat(end)
    step = relativedelta(**{units: interval})
    out = []
    while t0 < end:
        t1 = min(t0 + step, end)
        out.append((f"{t0}-to-{t1}", str(t0), str(t1)))
        t0 = t1
    return out


def windows_sql(windows):
    """A VALUES table of the windows, to join shots on absolute_time."""
    rows = ", ".join(
        f"('{label}', TIMESTAMPTZ '{t0} 00:00:00+00', TIMESTAMPTZ '{t1} 00:00:00+00')"
        for label, t0, t1 in windows
    )
    return f"(VALUES {rows}) AS w(time_window, w_start, w_end)"
