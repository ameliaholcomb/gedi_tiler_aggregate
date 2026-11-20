# gedi_tiler_aggregate (`gtagg`)

[GEDI-H3](https://github.com/tiagodc/GEDI-H3)'s `gh3_aggregate`,
`gh3_rasterize` and `gh3_from_img`, reimplemented in DuckDB over the
[tiled GEDI database](https://github.com/ameliaholcomb/gedi_tiler) on MAAP,
and run either in your workspace or as DPS jobs.

There is no Dask and no granule download: each unit of work is one DuckDB
query over the few 1°×1° database tiles it needs, so memory stays at a few
GB for any region size, and work splits cleanly across DPS jobs.

## Install

```bash
git clone git@github.com:ameliaholcomb/gedi_tiler_aggregate.git
cd gedi_tiler_aggregate
conda env create -f environment.yml   # env "gtagg"
conda activate gtagg
pip install -e .
```

Install rasterio from conda-forge (environment.yml does): the pip wheel
bundles its own GDAL/PROJ, which clash with DuckDB's spatial extension.

## Quick start

```bash
# Mean and 95th percentile RH98 on the 1 km EGI grid (as GEDI L4B), quality filtered
gtagg aggregate -o s3://maap-ops-workspace/$USER/rh98_conus \
    -egi 6 -l rh98=rh_l2a[99] -a "['mean','p95']" -y -r conus.gpkg --dps 20

gtagg status s3://maap-ops-workspace/$USER/rh98_conus       # partitions finished
gtagg submit s3://maap-ops-workspace/$USER/rh98_conus --n_jobs 5   # retry failures
gtagg rasterize s3://maap-ops-workspace/$USER/rh98_conus -o rh98_tifs/ --merge
```

Without `--dps`, `aggregate` computes everything in your workspace, which is
fine for regions up to a few hundred tiles. See `notebooks/quickstart.ipynb`
for all three tools, and `gtagg <command> -h` for every option.

| Command | GEDI-H3 | What it does |
|---|---|---|
| `gtagg aggregate` | `gh3_aggregate` | Aggregate shots to EGI squares (`-egi LEVEL`) or H3 hexagons (`-h3 LEVEL`), optionally per time window (`-ti/-tu`) |
| `gtagg rasterize` | `gh3_rasterize` | GeoTIFFs of an aggregated dataset, per partition or merged (`-m`) |
| `gtagg from_img` | `gh3_from_img` | Sample raster values (and window statistics, `-w`) at each shot |
| `gtagg status / submit / run` | | Progress; DPS (re)submission; compute unfinished partitions here |
| `gtagg merge` | `-m` | All of a dataset's partitions in one parquet file |

## How it works

`aggregate` and `from_img` first **plan** a dataset directory (local or
`s3://`): `_spec.json` (the options), `_plan.json` (each partition and
the database tiles it reads) and `_region.parquet`. A **partition** is a
coarse cell: EGI level 12 (~160 km) or H3 level 3 (~12,400 km², the
database's `h3_03` column). Every output cell lies in exactly one partition,
so each partition's statistics (medians and percentiles included) are exact
without any merge between jobs.

Each partition is then written to `parts/<id>.parquet` (or
`parts/<id>.empty` when it has no shots). Finished partitions are skipped,
so any run, local or DPS, can be repeated until `status` reports all done.
`--dps N` splits the unfinished partitions across N jobs.

**How many jobs?** Memory doesn't depend on the number of jobs: each job
computes one partition at a time, reading only that partition's 1–6 database
tiles, in a few GB. Choose N for wall time. An EGI partition took 3–8 s on a
16-core workspace in testing; `gtagg aggregate --plan_only` prints the
partition count before anything runs.

## Options

- **Variables (`-l`)** are database columns or `name=SQL`, e.g.
  `rh98=rh_l2a[99]` (SQL lists are 1-indexed) or `agbd_l4a`. The database's
  columns carry a product suffix (`_l2a`, `_l4a`, ...).
- **Aggregations (`-a`)**: `mean median std var min max sum count nunique
  first last pNN`, as one function, a list, a `{variable: function(s)}`
  dict, or a .json/.txt file. Columns are named `{variable}_{function}`
  (`pNN` → `{variable}_percentile_NN`); every dataset also gets `shot_count`.
- **Quality (`-y`)**: GEDI-H3's release 3 filters for every product the
  variables and query use, plus L2A's (L2A: `l2a_quality_flag_rel3 = 1`,
  `degrade_flag = 0`; L2B: `l2b_quality_flag_rel3 = 1`; L4A:
  `l4a_quality_flag_rel3 = 1`, `degrade_include_flag = 1`,
  `elev_highestreturn_outlier_flag = 0`; L4C: `l4c_quality_flag_rel3 = 1`).
- **Query (`-q`)** is a SQL condition, e.g. `-q "sensitivity_l2a > 0.98"`.
- **Region (`-r`)**: a vector file in any CRS, or a box `-r=W,S,E,N` (with
  `=`, since the box starts with a minus sign).
- **Time**: `-t0`/`-t1` (both days included); time series with
  `-ti 1 -tu years` (windows run from `-t0` to `-t1`, each ending where the next starts).

## Outputs

- **aggregate**: per cell, `egiNN` (GEDI-H3's EGI hash) or `h3_NN` (H3
  index), the partition id (`egi12` / `h3_03`), `time_window` for a time
  series, the aggregates, `shot_count`, and the cell polygon as `geometry`
  (GeoParquet, lon/lat).
- **rasterize**: float32 GeoTIFFs, one band per column. EGI is native in
  EPSG:6933 (level 6 is the GEDI L4B grid). H3 is drawn on a global
  EPSG:4326 grid, with pixels twice the hexagons' mean edge length, each
  taking the hexagon at its centre. Per partition (EGI) or tile (H3), or one
  mosaic per time window with `--merge`.
- **from_img**: per shot, `shot_number`, `h3_03`, one column per band, one
  per window op (`{band}_{op}_{Z}x{Z}`), and with `-g` its `geometry`.

## Differences from GEDI-H3

Deliberate, where GEDI-H3's behaviour seemed wrong or awkward:

- **`-t1` includes the end day.** GEDI-H3 compares against the first instant
  of `-t1`, dropping that day.
- **Window ops ignore NoData.** GEDI-H3 runs scipy filters over the whole
  raster, where NoData pixels (and `mode='nearest'` edge padding) leak into
  neighbouring windows. Here each statistic uses only the valid pixels in
  the window, and is NaN when there are none. Mode ties go to the smallest value.
- **Queries are SQL, not pandas** (`-q "a > 1 AND b = 0"`), and variables
  can be SQL expressions, so profile elements like `rh_l2a[99]` need no
  flattened `rh_098` column.
- **Partitions are derived from the output cell**, so a cell carried
  across a 160 km EGI tile edge (GEDI-H3's float carry) never splits
  between two partitions.
- **One file layout for all outputs**: a dataset directory of per-partition
  GeoParquet files, plus GeoTIFFs only from `rasterize`. There is no
  `aggregate -R`; run `rasterize` afterwards (H3 rasters need neighbouring
  partitions, which a single job doesn't have).
- **`shot_count` is always added**, and H3 rasters are sampled at pixel
  centres rather than interpolated (no geocube/UTM round trip).
- Rasterized GeoTIFFs are tiled GeoTIFFs, not COGs; ISO3 country codes as
  regions, `gh3_from_polygon`, `--fillna` before aggregation and custom
  Python aggregation functions are not supported.

## DPS

The algorithm `gedi-tiler-aggregate` (`dps/algorithm_config.yml`) takes a
dataset, a submission tag and a job index; `gtagg submit` writes each
submission's split to `<dataset>/_jobs/<tag>.json`. The build installs
`environment.yml`, this package, and DuckDB's h3 extension. Datasets for DPS
must be on S3. Register a new version with MAAP's algorithm registration
(e.g. `commit_and_run.py dps/algorithm_config.yml`), then pass it as
`--algo_version`.

## Tests

```bash
pytest tests
```
`tests/test_grids.py` checks the SQL EGI hashes against a numpy port of
GEDI-H3's algorithm, including points on pixel and tile edges.
