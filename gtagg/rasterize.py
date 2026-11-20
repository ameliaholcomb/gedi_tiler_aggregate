"""GeoTIFFs of an aggregated dataset (GEDI-H3's gh3_rasterize).

EGI datasets rasterize natively: one pixel per cell, in EASE-Grid 2.0
(EPSG:6933). H3 datasets are drawn on a global lon/lat (EPSG:4326) grid
with pixels twice the hexagons' mean edge length, as in GEDI-H3; each
pixel takes the value of the hexagon holding its centre.

Rasters are written per unit (an EGI partition, or a 1 x 1 degree tile
for H3), with one band per column, all on one global pixel grid, so
--merge can mosaic them into a single file. A time-series dataset gets
one set of rasters per time window.
"""

import math
import os
import tempfile

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from gtiler.database import ducky

from gtagg import grids, store
from gtagg.spec import agg_columns

GTIFF = dict(driver="GTiff", dtype="float32", nodata=np.nan, tiled=True,
             blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER")


def _egi_unit(con, grid, part, columns):
    df = con.sql(f"SELECT * FROM '{part}'").df()
    _, xo, yo, xi, yi = grids.egi_decode(df[grid.cell_col].values)
    df["col"] = xo.astype(np.int64) * grid.per_tile + xi.astype(np.int64)
    df["row"] = -(yo.astype(np.int64) * grid.per_tile + yi.astype(np.int64))  # north up
    return df


def _egi_transform(grid, col0, row0):
    # Pixels counted from the grid's south-west corner; row = -(row from south).
    return from_origin(grids.EGI_X0 + col0 * grid.res, grids.EGI_Y0 + (1 - row0) * grid.res,
                       grid.res, grid.res)


def _h3_unit(con, grid, parts, tile):
    west, south, east, north = grids.tile_box(tile)
    res = grid.res
    # The pixels whose centres are in the tile, so tiles never share pixels.
    c0, c1 = (math.ceil((v + 180) / res - 0.5) for v in (west, east))
    r0, r1 = (math.ceil((90 - v) / res - 0.5) for v in (north, south))
    files = ", ".join(f"'{p}'" for p in parts)
    return con.sql(f"""
        WITH px AS (
            SELECT r AS row, c AS col,
                   h3_latlng_to_cell(90 - (r + 0.5) * {res}, -180 + (c + 0.5) * {res},
                                     {grid.level})::UBIGINT AS cell
            FROM range({r0}, {r1}) t1(r), range({c0}, {c1}) t2(c)
        )
        SELECT px.row, px.col, d.*
        FROM px JOIN read_parquet([{files}]) d ON d.{grid.cell_col}::UBIGINT = px.cell
    """).df()


def _h3_transform(grid, col0, row0):
    return from_origin(-180 + col0 * grid.res, 90 - row0 * grid.res, grid.res, grid.res)


def _write(path, df, columns, crs, transform_of, compress):
    """A GeoTIFF of the pixels in df (global `row`, `col`), cropped to them."""
    row0, col0 = df.row.min(), df.col.min()
    h, w = df.row.max() - row0 + 1, df.col.max() - col0 + 1
    r, c = (df.row - row0).values, (df.col - col0).values
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(path, "w", width=w, height=h, count=len(columns), crs=crs,
                       transform=transform_of(col0, row0), compress=compress, **GTIFF) as dst:
        for i, col in enumerate(columns, start=1):
            band = np.full((h, w), np.nan, dtype="float32")
            band[r, c] = df[col].values
            dst.write(band, i)
            dst.set_band_description(i, col)


def mosaic(paths, out, compress):
    """Merge rasters on one pixel grid (and not overlapping) into one file."""
    with rasterio.open(paths[0]) as src:
        profile, res = src.profile, src.res[0]
        descriptions = src.descriptions
    bounds = []
    for p in paths:
        with rasterio.open(p) as src:
            bounds.append(src.bounds)
    west = min(b.left for b in bounds)
    north = max(b.top for b in bounds)
    width = round((max(b.right for b in bounds) - west) / res)
    height = round((north - min(b.bottom for b in bounds)) / res)
    profile.update(width=width, height=height, transform=from_origin(west, north, res, res),
                   compress=compress, SPARSE_OK=True)
    with rasterio.open(out, "w", **profile) as dst:
        for i, d in enumerate(descriptions, start=1):
            dst.set_band_description(i, d)
        for p in paths:
            with rasterio.open(p) as src:
                window = Window(round((src.bounds.left - west) / res),
                                round((north - src.bounds.top) / res),
                                src.width, src.height)
                dst.write(src.read(), window=window)


def rasterize(dataset, output, columns=None, merge=False, compress="LZW"):
    spec = store.read_json(store.join(dataset, "_spec.json"))
    if spec["tool"] != "aggregate":
        raise SystemExit("rasterize needs a dataset made by `gtagg aggregate`")
    plan = store.read_json(store.join(dataset, "_plan.json"))
    grid = grids.make_grid(spec["grid"], spec["level"])
    missing = set(plan) - store.done_partitions(dataset)
    done = store.data_partitions(dataset)
    if missing:
        print(f"WARNING: {len(missing)} of {len(plan)} partitions are not computed yet; "
              "their areas will be empty")
    con = ducky.init_duckdb(temp_dir="/tmp/duckdb")
    con.sql("SET enable_progress_bar = false")
    con.install_extension("h3", repository="community")
    con.load_extension("h3")
    if columns is None:
        columns = list(agg_columns({v: v for v in spec["variables"]}, spec["aggregate"]))
        columns.append("shot_count")

    if grid.kind == "egi":
        crs, transform_of = grids.EGI_CRS, lambda c, r: _egi_transform(grid, c, r)
        units = {pid: [store.part_path(dataset, pid)] for pid in sorted(done)}
    else:
        crs, transform_of = "EPSG:4326", lambda c, r: _h3_transform(grid, c, r)
        units = {}
        for pid, tiles in plan.items():
            if pid in done:
                for t in tiles:
                    units.setdefault(t, []).append(store.part_path(dataset, pid))
    labels = [w[0] for w in spec["windows"]] or [None]

    with tempfile.TemporaryDirectory(prefix="gtagg_") as tmp:
        _rasterize_units(con, grid, units, labels, columns, crs, transform_of,
                         tmp if merge else output, output, merge, compress)


def _rasterize_units(con, grid, units, labels, columns, crs, transform_of,
                     base, output, merge, compress):
    written = {label: [] for label in labels}
    for i, (unit, parts) in enumerate(sorted(units.items())):
        ducky.refresh_credentials(con)
        df = _egi_unit(con, grid, parts[0], columns) if grid.kind == "egi" \
            else _h3_unit(con, grid, parts, unit)
        for label in labels:
            sub = df if label is None else df[df.time_window == label]
            if sub.empty:
                continue
            path = os.path.join(base, *([label] if label else []), f"{unit}.tif")
            _write(path, sub, columns, crs, transform_of, compress)
            written[label].append(path)
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(units)} units", flush=True)

    if merge:
        os.makedirs(output, exist_ok=True)
        for label, paths in written.items():
            if paths:
                out = os.path.join(output, f"{label or 'merged'}.tif")
                mosaic(paths, out, compress)
                print(f"Wrote {out}")
    else:
        print(f"Wrote {sum(map(len, written.values()))} rasters to {output}")
