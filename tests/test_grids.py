"""The SQL EGI hashes agree with a numpy reference of GEDI-H3's to_hash."""

import duckdb
import numpy as np
import pytest

from gtagg import grids


def reference_hash(x, y, level):
    # GEDI-H3's algorithm (gedih3.egi.core.to_hash), in numpy.
    scale = grids.EGI_RES[level]
    per_tile = round(grids.EGI_OUTER / scale)
    out = []
    for coord, origin in ((x, grids.EGI_X0), (y, grids.EGI_Y0)):
        offset = coord - origin
        outer = (offset // grids.EGI_OUTER).astype(np.int64)
        inner = (offset % grids.EGI_OUTER // scale).astype(np.int64)
        carry = inner >= per_tile
        out += [np.where(carry, outer + 1, outer), np.where(carry, 0, inner)]
    xo, xi, yo, yi = out
    return (
        np.uint64(level) * np.uint64(10**18)
        + xo.astype(np.uint64) * np.uint64(10**15)
        + yo.astype(np.uint64) * np.uint64(10**12)
        + xi.astype(np.uint64) * np.uint64(10**6)
        + yi.astype(np.uint64)
    )


def points():
    rng = np.random.default_rng(0)
    x = rng.uniform(grids.EGI_X0, -grids.EGI_X0, 100_000)
    y = rng.uniform(grids.EGI_Y0, -grids.EGI_Y0, 100_000)
    # Points exactly on pixel and tile edges, where float error matters.
    for level in (3, 6, 7, 12):
        res = grids.EGI_RES[level]
        kx = rng.integers(0, int(-2 * grids.EGI_X0 / res), 2000)
        ky = rng.integers(0, int(-2 * grids.EGI_Y0 / res), 2000)
        x = np.concatenate([x, grids.EGI_X0 + kx * res])
        y = np.concatenate([y, grids.EGI_Y0 + ky * res])
    return x, y


@pytest.mark.parametrize("level", range(1, 13))
def test_egi_hash_matches_reference(level):
    x, y = points()
    con = duckdb.connect()
    con.register("pts", {"x": x, "y": y})
    sql = grids.egi_hash_sql(level)
    sql = sql.replace("ST_X(geometry_6933)", "x").replace("ST_Y(geometry_6933)", "y")
    got = con.sql(f"SELECT {sql} AS h FROM pts").fetchnumpy()["h"].astype(np.uint64)
    want = reference_hash(x, y, level)
    bad = np.flatnonzero(got != want)
    assert len(bad) == 0, list(zip(x[bad][:3], y[bad][:3], got[bad][:3], want[bad][:3]))


@pytest.mark.parametrize("level", [3, 6, 9, 12])
def test_partition_holds_whole_cells(level):
    # A partition's filter selects exactly the cells whose own tile indices
    # are that partition's (GEDI-H3's to_parent).
    x, y = points()
    con = duckdb.connect()
    con.register("pts", {"x": x, "y": y})
    grid = grids.EGI(level)
    cell = grid.cell_sql().replace("ST_X(geometry_6933)", "x").replace("ST_Y(geometry_6933)", "y")
    con.sql(f"CREATE TABLE cells AS SELECT {cell} AS c FROM pts")
    ids = con.sql("SELECT c FROM cells").fetchnumpy()["c"].astype(np.uint64)
    _, xo, yo, _, _ = grids.egi_decode(ids)
    pids = grids.egi_partition_id(xo.astype(object), yo.astype(object))
    for pid in sorted(set(pids))[:20]:
        got = con.sql(f"SELECT count(*) FROM cells WHERE {grid.in_partition('c', pid)}").fetchone()[0]
        assert got == sum(p == pid for p in pids)
