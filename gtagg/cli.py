"""Command-line tools, after GEDI-H3's gh3_* tools.

    gt_aggregate  -o DATASET -egi 6 -l rh98=rh_l2a[99] -a mean -y -r region.gpkg [--dps N]
    gt_from_img   -o DATASET -i image.tif -y [--dps N]
    gt_rasterize  DATASET -o rasters/ --merge
    gt_merge      DATASET -o all.parquet
    gt_run        DATASET            (what DPS jobs run; also resumes locally)

gt_aggregate and gt_from_img plan a DATASET (a local or s3:// directory),
then compute it here, or as N DPS jobs with --dps N. Run the same command
again to resume: it computes (or submits) only the unfinished partitions,
and exits at once when there are none.
"""

import argparse
import shlex

DATABASE = "s3://maap-ops-workspace/shared/ameliah/tiled_gedi_v3"


def _parse(parser, argv):
    if isinstance(argv, str):
        argv = shlex.split(argv)
    return parser.parse_args(argv)


def _shot_args(p):
    p.add_argument("-o", "--output", required=True,
                   help="dataset directory to write, local or s3:// (s3:// for --dps)")
    p.add_argument("-r", "--region", help="vector file, or bbox -r=W,S,E,N [default: everywhere]")
    p.add_argument("-y", "--quality", action="store_true",
                   help="apply GEDI-H3's quality filters for the products used")
    p.add_argument("-q", "--query", help="SQL filter on the database's columns, e.g. "
                   "\"sensitivity_l2a > 0.95\"")
    p.add_argument("--beam-type", dest="beam_type", choices=["power", "coverage"])
    p.add_argument("-t0", "--time-start", dest="time_start", help="start date [YYYY-MM-DD]")
    p.add_argument("-t1", "--time-end", dest="time_end",
                   help="end date [YYYY-MM-DD]; as in GEDI-H3, up to its first instant")
    p.add_argument("--database", default=DATABASE, help=f"tiled GEDI database [{DATABASE}]")
    p.add_argument("--plan_only", action="store_true",
                   help="only plan the dataset (prints its number of partitions)")
    p.add_argument("--threads", type=int, help="DuckDB threads, when running here")
    d = p.add_argument_group("DPS")
    d.add_argument("--dps", type=int, metavar="N_JOBS",
                   help="compute the unfinished partitions as this many DPS jobs")
    d.add_argument("--batch", help="DPS batch name [gt_<dataset name>]; each submission "
                   "appends a generation number")
    d.add_argument("--algo_version", default="main", help="DPS algorithm version [main]")
    d.add_argument("--queue", default="maap-dps-worker-8gb", help="DPS queue")


def _base_spec(args, tool):
    return {
        "tool": tool,
        "output": args.output.rstrip("/"),
        "database": args.database,
        "quality": args.quality,
        "query": args.query,
        "beam_type": args.beam_type,
        "time_start": args.time_start,
        "time_end": args.time_end,
        "windows": [],
        "variables": {},
    }


def _plan_and_run(args, spec):
    from gtagg import dps, plan, run, store

    region = plan.read_region(args.region) if args.region else None
    partitions = plan.make_plan(spec, region)
    if args.plan_only:
        return
    todo = sorted(set(partitions) - store.done_partitions(spec["output"]))
    if not todo:
        print(f"All {len(partitions)} partitions are finished.")
        return
    if len(todo) < len(partitions):
        print(f"{len(partitions) - len(todo)}/{len(partitions)} partitions are finished.")
    if args.dps:
        dps.submit(spec["output"], todo, args.dps, args.batch, args.algo_version, args.queue)
    else:
        run.run(spec["output"], todo, threads=args.threads)


def gt_aggregate(argv=None):
    """Aggregate GEDI shots to EGI squares or H3 hexagons (GEDI-H3's gh3_aggregate)."""
    from gtagg import spec as specs

    p = argparse.ArgumentParser(prog="gt_aggregate", description=gt_aggregate.__doc__)
    _shot_args(p)
    p.add_argument("-egi", "--egi", type=int, nargs="?", const=6, metavar="LEVEL",
                   help="EGI level 1-12 (6 = 1 km, as GEDI L4B) [bare flag: 6]")
    p.add_argument("-h3", "--h3-level", dest="h3_level", type=int, help="H3 level 0-15")
    p.add_argument("-l", "--list", nargs="+", required=True,
                   help="variables: database columns, or name=SQL, e.g. rh98=rh_l2a[99]")
    p.add_argument("-a", "--aggregate", default="mean",
                   help="'mean', \"['mean','std','p95']\", \"{'rh98':['mean','count']}\", "
                        "or a .json/.txt file of one [default: mean]. Functions: mean "
                        "median std var min max sum count nunique first last pNN")
    p.add_argument("-ti", "--time-interval", dest="time_interval", type=int, default=0,
                   help="time series: one output per window of this many units")
    p.add_argument("-tu", "--time-units", dest="time_units", default="years",
                   choices=["years", "months", "weeks", "days"])
    args = _parse(p, argv)

    if (args.h3_level is None) == (args.egi is None):
        p.error("give one of -egi LEVEL or -h3 LEVEL")
    spec = _base_spec(args, "aggregate")
    spec["grid"], spec["level"] = ("egi", args.egi) if args.egi else ("h3", args.h3_level)
    spec["variables"] = specs.parse_variables(args.list)
    spec["aggregate"] = specs.parse_aggregation(args.aggregate)
    specs.agg_columns(spec["variables"], spec["aggregate"])  # check it now
    if args.time_interval:
        if not (args.time_start and args.time_end):
            p.error("a time series (-ti) needs -t0 and -t1")
        spec["windows"] = specs.time_windows(
            args.time_start, args.time_end, args.time_interval, args.time_units)
    _plan_and_run(args, spec)


def gt_from_img(argv=None):
    """Sample raster values at GEDI shots (GEDI-H3's gh3_from_img)."""
    from gtagg import sample

    p = argparse.ArgumentParser(prog="gt_from_img", description=gt_from_img.__doc__)
    _shot_args(p)
    p.add_argument("-i", "--image", required=True, help="raster file or VRT, local or s3://")
    p.add_argument("-B", "--bands", nargs="+", type=int, help="0-based bands [default: all]")
    p.add_argument("-b", "--band-names", dest="band_names", nargs="+",
                   help="names for the selected bands' columns")
    p.add_argument("-w", "--window", nargs="+",
                   help="window ops 'BZO': band B, odd size Z, op O (0 sum, 1 mean, "
                        "2 median, 3 mode, 4 std, 5 min, 6 max, 7 count, 8 range)")
    p.add_argument("-F", "--fillna", type=float, help="value for NoData pixels")
    p.add_argument("--dropna", action="store_true", help="drop shots with no band values")
    p.add_argument("-g", "--geo", action="store_true", help="include shot geometry")
    args = _parse(p, argv)

    sample.parse_window_specs(args.window)  # check it now
    spec = _base_spec(args, "from_img")
    # Partition on the database's h3_03 cells, the cheapest to filter on.
    spec["grid"], spec["level"] = "h3", 3
    spec["image"] = {
        "path": args.image, "bands": args.bands, "band_names": args.band_names,
        "window": args.window, "fillna": args.fillna, "dropna": args.dropna, "geo": args.geo,
    }
    if args.region is None:
        args.region = _image_bbox(args.image)
    _plan_and_run(args, spec)


def _image_bbox(image):
    import rasterio
    from rasterio.warp import transform_bounds

    from gtagg import sample

    with sample.raster_env(), rasterio.open(image) as src:
        w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    return f"{w},{s},{e},{n}"


def gt_rasterize(argv=None):
    """GeoTIFFs of a gt_aggregate dataset (GEDI-H3's gh3_rasterize)."""
    from gtagg import rasterize

    p = argparse.ArgumentParser(prog="gt_rasterize", description=gt_rasterize.__doc__)
    p.add_argument("dataset")
    p.add_argument("-o", "--output", required=True, help="local directory for the GeoTIFFs")
    p.add_argument("-l", "--list", nargs="+", help="columns to rasterize [default: all]")
    p.add_argument("-m", "--merge", action="store_true", help="mosaic into one file per window")
    p.add_argument("--compress", default="LZW", choices=["LZW", "ZSTD", "DEFLATE", "NONE"])
    args = _parse(p, argv)
    rasterize.rasterize(args.dataset.rstrip("/"), args.output, args.list,
                        args.merge, args.compress)


def gt_merge(argv=None):
    """All of a dataset's partitions in one (Geo)Parquet file (GEDI-H3's -m)."""
    from gtiler.database import ducky

    from gtagg import store

    p = argparse.ArgumentParser(prog="gt_merge", description=gt_merge.__doc__)
    p.add_argument("dataset")
    p.add_argument("-o", "--output", required=True, help="parquet file, local or s3://")
    args = _parse(p, argv)
    con = ducky.init_duckdb(temp_dir="/tmp/duckdb")
    con.sql(f"COPY (FROM '{store.join(args.dataset, 'parts')}/*.parquet') TO '{args.output}'")
    print(f"Wrote {args.output}")


def gt_run(argv=None):
    """Compute a dataset's unfinished partitions here: what each DPS job runs."""
    from gtagg import dps, run

    p = argparse.ArgumentParser(prog="gt_run", description=gt_run.__doc__)
    p.add_argument("dataset")
    p.add_argument("--tag", help="a DPS submission's tag: compute only job --job's share")
    p.add_argument("--job", type=int)
    p.add_argument("--threads", type=int)
    args = _parse(p, argv)
    dataset = args.dataset.rstrip("/")
    parts = dps.job_partitions(dataset, args.tag, args.job) if args.tag else None
    run.run(dataset, parts, threads=args.threads)
