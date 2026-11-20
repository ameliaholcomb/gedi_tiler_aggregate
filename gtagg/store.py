"""Files in an output dataset, which may be on S3 or local disk.

A dataset directory holds:
    _spec.json       what to compute (the tool's options)
    _plan.json       {partition id: [database tile ids it reads]}
    _region.parquet  the region (GeoParquet, lon/lat), if one was given
    parts/<partition id>.parquet   one file per finished partition
    parts/<partition id>.empty     instead, for a partition with no shots
Finished partitions are skipped on reruns, so any run can be repeated.
"""

import json
import os

import fsspec
import geopandas as gpd

from gtiler.database import ducky


def fs_path(path):
    return fsspec.core.url_to_fs(path)


def join(*parts):
    return "/".join(p.rstrip("/") for p in parts)


def read_json(path):
    with fsspec.open(path) as f:
        return json.load(f)


def write_json(path, obj):
    fs, p = fs_path(path)
    fs.makedirs(os.path.dirname(p), exist_ok=True)
    with fs.open(p, "w") as f:
        json.dump(obj, f, indent=1)


def exists(path):
    fs, p = fs_path(path)
    return fs.exists(p)


def put(local, path):
    fs, p = fs_path(path)
    fs.makedirs(os.path.dirname(p), exist_ok=True)
    fs.put_file(local, p)


def write_region(dataset, gdf):
    fs, p = fs_path(join(dataset, "_region.parquet"))
    fs.makedirs(os.path.dirname(p), exist_ok=True)
    with fs.open(p, "wb") as f:
        gdf[["geometry"]].to_parquet(f)


def read_region(dataset):
    """The region as one shapely geometry, or None if there is none."""
    path = join(dataset, "_region.parquet")
    if not exists(path):
        return None
    with fsspec.open(path) as f:
        return gpd.read_parquet(f).union_all()


def part_path(dataset, pid):
    return join(dataset, "parts", f"{pid}.parquet")


def _parts(dataset):
    fs, p = fs_path(join(dataset, "parts"))
    if not fs.exists(p):
        return []
    return [os.path.basename(f) for f in fs.ls(p, refresh=True)]


def done_partitions(dataset):
    """Ids of the finished partitions, with data or empty."""
    return {f.split(".")[0] for f in _parts(dataset) if f.endswith((".parquet", ".empty"))}


def data_partitions(dataset):
    """Ids of the finished partitions that hold data."""
    return {f.removesuffix(".parquet") for f in _parts(dataset) if f.endswith(".parquet")}


def connect(database, memory_limit="6GB", threads=None):
    """DuckDB with the tiled GEDI database attached as `gedi_dl`."""
    con = ducky.init_duckdb(temp_dir="/tmp/duckdb")
    con.sql("SET enable_progress_bar = false")
    con.sql(f"SET memory_limit = '{memory_limit}'")
    if threads:
        con.sql(f"SET threads = {threads}")
    con.install_extension("h3", repository="community")
    con.load_extension("h3")
    bucket, prefix = database.removeprefix("s3://").split("/", 1)
    ducky.attach_ducklake(con, bucket, prefix.rstrip("/"), name="gedi_dl")
    return con
