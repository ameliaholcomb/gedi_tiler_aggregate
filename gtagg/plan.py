"""Plan a run: which partitions to compute and which database tiles each reads."""

import geopandas as gpd
import shapely

from gtiler.database import ducky

from gtagg import grids, store


def read_region(region):
    """A GeoDataFrame from a vector file or a 'W,S,E,N' box."""
    parts = region.split(",")
    if len(parts) == 4:
        try:
            box = shapely.box(*map(float, parts))
        except ValueError:
            pass
        else:
            return gpd.GeoDataFrame(geometry=[box], crs="EPSG:4326")
    return gpd.read_file(region)


def database_tiles(con):
    """Ids of the tiles that hold data."""
    return {r[0] for r in con.sql(r"""
        SELECT DISTINCT regexp_extract(data_file, 'tile_id=([NS]\d\d_[EW]\d\d\d)', 1)
        FROM ducklake_list_files('gedi_dl', 'data')
    """).fetchall()}


def plan_partitions(grid, tile_ids):
    """{partition id: sorted tile ids whose shots may fall in it}."""
    plan = {}
    for tile in tile_ids:
        for pid in grid.partitions_in_box(*grids.tile_box(tile)):
            plan.setdefault(str(pid), []).append(tile)
    return {pid: sorted(tiles) for pid, tiles in sorted(plan.items())}


def make_plan(con, spec, region_gdf=None):
    """Write the spec, plan and region of a new dataset, or check that an
    existing one was planned with the same spec. Returns the plan."""
    out = spec["output"]
    if store.exists(store.join(out, "_spec.json")):
        old = store.read_json(store.join(out, "_spec.json"))
        if old != spec:
            raise SystemExit(
                f"{out} already holds a dataset made with different options. "
                "Use a new --output, or delete the old one first."
            )
        print(f"Resuming the dataset in {out}")
        return store.read_json(store.join(out, "_plan.json"))

    tiles = database_tiles(con)
    if region_gdf is not None:
        tiles &= set(ducky.covering_tile_ids(region_gdf))
    grid = grids.make_grid(spec["grid"], spec["level"])
    plan = plan_partitions(grid, tiles)
    if region_gdf is not None:
        store.write_region(out, region_gdf.to_crs("EPSG:4326"))
    store.write_json(store.join(out, "_plan.json"), plan)
    store.write_json(store.join(out, "_spec.json"), spec)
    print(f"Planned {len(plan)} partitions over {len(tiles)} database tiles in {out}")
    return plan
