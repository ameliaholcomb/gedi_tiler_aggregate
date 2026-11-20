"""Compute partitions of a planned dataset: what each DPS job runs.

Every partition is one DuckDB query over only the database tiles it
touches, written to parts/<partition id>.parquet. Memory stays at a few
GB whatever the size of the region, and finished partitions are skipped,
so a failed or cancelled run can simply be run again.
"""

import os
import tempfile
import time

import rasterio
import shapely

from gtiler.database import ducky

from gtagg import grids, sample, spec as specs, store


def _region_clip(region, tiles):
    """The region's part in these tiles as WKT, or None when it covers them
    (or there is no region), so that no per-shot test is needed."""
    if region is None:
        return None
    box = grids.tiles_union(tiles)
    if region.covers(box):
        return None
    return shapely.to_wkt(region.intersection(box))


def shots_sql(spec, grid, pid, tiles, columns, region_wkt, select):
    """SQL for the filtered shots of one partition, with `cell` (the target
    cell id), `time_window` (time series only) and the `select` expressions."""
    conds = specs.where_sql(spec, columns)
    tile_list = ", ".join(f"'{t}'" for t in tiles)
    conds.append(f"tile_id IN ({tile_list})")
    if region_wkt:
        conds.append(f"ST_Intersects(geometry, ST_GeomFromText('{region_wkt}'))")
    join = ""
    if spec["windows"]:
        join = (f"JOIN {specs.windows_sql(spec['windows'])} "
                "ON absolute_time >= w_start AND absolute_time < w_end")
        select = select + ["time_window"]
    return f"""
        SELECT * FROM (
            SELECT {grid.cell_sql()} AS cell, {", ".join(select)}
            FROM gedi_dl.data {join}
            WHERE {" AND ".join(conds)}
        )
        WHERE {grid.in_partition("cell", pid)}
    """


def cell_geometry_sql(grid):
    """SQL for the polygon of the cell `cell`, in lon/lat."""
    if grid.kind == "h3":
        return "ST_GeomFromText(h3_cell_to_boundary_wkt(cell))"
    # A box in EASE-Grid 2.0 is a box in lon/lat: the grid is cylindrical.
    x = (f"{grids.EGI_X0} + (cell % 1000000000000000000 // 1000000000000000) * {grids.EGI_OUTER}"
         f" + (cell % 1000000000000 // 1000000) * {grid.res}")
    y = (f"{grids.EGI_Y0} + (cell % 1000000000000000 // 1000000000000) * {grids.EGI_OUTER}"
         f" + (cell % 1000000) * {grid.res}")
    return (f"ST_Transform(ST_MakeEnvelope({x}, {y}, {x} + {grid.res}, {y} + {grid.res}), "
            f"'{grids.EGI_CRS}', 'OGC:CRS84', always_xy := true)")


def aggregate_partition(con, spec, grid, pid, tiles, columns, region_wkt, out):
    names = list(spec["variables"])
    select = [f"{expr} AS {name}" for name, expr in spec["variables"].items()]
    aggs = specs.agg_columns({n: n for n in names}, spec["aggregate"])
    by = "cell, time_window" if spec["windows"] else "cell"
    con.sql(f"""
        COPY (
            SELECT {pid}::{grid.id_type} AS {grid.partition_col},
                   cell AS {grid.cell_col},
                   * EXCLUDE (cell),
                   {cell_geometry_sql(grid)} AS geometry
            FROM (
                SELECT {by},
                       {", ".join(f"{sql} AS {col}" for col, sql in aggs.items())},
                       count(*) AS shot_count
                FROM ({shots_sql(spec, grid, pid, tiles, columns, region_wkt, select)})
                GROUP BY {by}
            )
        ) TO '{out}'
    """)


def from_img_partition(con, spec, grid, pid, tiles, columns, region_wkt, out):
    select = ["shot_number", "lon_lowestmode", "lat_lowestmode"]
    df = con.sql(shots_sql(spec, grid, pid, tiles, columns, region_wkt, select)).df()
    img = spec["image"]
    with sample.raster_env(), rasterio.open(img["path"]) as src:
        bands = img["bands"] if img["bands"] is not None else list(range(src.count))
        names = sample.band_names(img["path"], src, bands, img["band_names"])
        windows = sample.parse_window_specs(img["window"])
        values = sample.sample(src, df.lon_lowestmode.values, df.lat_lowestmode.values,
                               bands, names, windows, img["fillna"])
    for col, v in values.items():
        df[col] = v
    if img["dropna"]:
        df = df[df[list(values)].notna().any(axis=1)]
    df[grid.partition_col] = pid
    keep = ["shot_number", grid.partition_col] + (["time_window"] if spec["windows"] else [])
    rel = con.from_df(df[keep + list(values) + ["lon_lowestmode", "lat_lowestmode"]])
    geo = ", ST_Point(lon_lowestmode, lat_lowestmode) AS geometry" if img["geo"] else ""
    con.sql(f"""
        COPY (SELECT * EXCLUDE (lon_lowestmode, lat_lowestmode){geo} FROM rel) TO '{out}'
    """)


def run(dataset, partitions=None, threads=None):
    """Compute the given partitions (default: all unfinished ones)."""
    spec = store.read_json(store.join(dataset, "_spec.json"))
    plan = store.read_json(store.join(dataset, "_plan.json"))
    region = store.read_region(dataset)
    grid = grids.make_grid(spec["grid"], spec["level"])
    done = store.done_partitions(dataset)
    todo = [p for p in (partitions or plan) if p not in done]
    if not todo:
        print("All partitions are finished.")
        return
    print(f"{len(todo)} partitions to compute", flush=True)

    con = store.connect(spec["database"], threads=threads)
    columns = {r[0] for r in con.sql("DESCRIBE gedi_dl.data").fetchall()}
    compute = {"aggregate": aggregate_partition, "from_img": from_img_partition}[spec["tool"]]
    for i, pid in enumerate(todo):
        t = time.time()
        ducky.refresh_credentials(con)  # MAAP credentials expire after ~1 hour
        tiles = plan[pid]
        out = store.part_path(dataset, pid)
        # Write locally, then upload in one piece: a part is complete or absent.
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "part.parquet")
            compute(con, spec, grid, pid, tiles, columns, _region_clip(region, tiles), local)
            if con.sql(f"SELECT count(*) FROM '{local}'").fetchone()[0] == 0:
                # No shots: mark it done, but keep empty files (whose
                # geometry column has no type) out of the dataset.
                open(local, "w").close()
                out = out.removesuffix(".parquet") + ".empty"
            store.put(local, out)
        print(f"{i + 1}/{len(todo)} partition {pid} ({len(tiles)} tiles) "
              f"in {time.time() - t:.0f}s", flush=True)
