"""gtagg: GEDI-H3's aggregate, rasterize and from_img on the tiled GEDI database.

    gtagg aggregate  -o DATASET -egi 6 -l rh98=rh_l2a[99] -a mean -y -r region.gpkg
    gtagg from_img   -o DATASET -i image.tif -r region.gpkg -y
    gtagg rasterize  DATASET -o rasters/ --merge
    gtagg status     DATASET
    gtagg submit     DATASET --n_jobs 20 --algo_version main
    gtagg run        DATASET            (compute unfinished partitions here)
    gtagg merge      DATASET -o all.parquet

aggregate and from_img plan a DATASET (a local or s3:// directory), then
compute it here, or on DPS with --dps N_JOBS. Run `gtagg <command> -h`.
"""

import argparse

DATABASE = "s3://maap-ops-workspace/shared/ameliah/tiled_gedi_v3"


def add_shot_args(p):
    p.add_argument("-o", "--output", required=True,
                   help="dataset directory to write, local or s3:// "
                        "(on DPS, it must be on S3)")
    p.add_argument("-r", "--region", help="vector file or bbox 'W,S,E,N' [default: everywhere]")
    p.add_argument("-y", "--quality", action="store_true",
                   help="apply GEDI-H3's quality filters for the products used")
    p.add_argument("-q", "--query", help="SQL filter on the database's columns, e.g. "
                   "\"sensitivity_l2a > 0.95 AND rh_l2a[99] < 100\"")
    p.add_argument("--beam-type", dest="beam_type", choices=["power", "coverage"])
    p.add_argument("-t0", "--time-start", dest="time_start", help="start date [YYYY-MM-DD]")
    p.add_argument("-t1", "--time-end", dest="time_end", help="end date, included [YYYY-MM-DD]")
    p.add_argument("--database", default=DATABASE, help=f"tiled GEDI database [{DATABASE}]")
    p.add_argument("--dps", type=int, metavar="N_JOBS",
                   help="run as this many DPS jobs instead of here")
    p.add_argument("--plan_only", action="store_true",
                   help="only plan the dataset; compute it later with run or submit")
    add_dps_args(p)
    p.add_argument("--threads", type=int, help="DuckDB threads, when running here")


def add_dps_args(p):
    p.add_argument("--algo_version", default="main", help="DPS algorithm version [main]")
    p.add_argument("--queue", default="maap-dps-worker-8gb", help="DPS queue")


def base_spec(args, tool):
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


def plan_and_run(args, spec):
    from gtagg import dps, plan, run, store

    region = plan.read_region(args.region) if args.region else None
    con = store.connect(spec["database"])
    plan.make_plan(con, spec, region)
    con.close()
    if args.plan_only:
        return
    if args.dps:
        dps.submit(spec["output"], args.dps, args.algo_version, args.queue)
    else:
        run.run(spec["output"], threads=args.threads)


def cmd_aggregate(args):
    from gtagg import spec as specs

    if (args.h3_level is None) == (args.egi is None):
        raise SystemExit("Give one of -egi LEVEL or -h3 LEVEL")
    spec = base_spec(args, "aggregate")
    spec["grid"], spec["level"] = ("egi", args.egi) if args.egi else ("h3", args.h3_level)
    spec["variables"] = specs.parse_variables(args.list)
    if not spec["variables"]:
        raise SystemExit("Give the variables to aggregate with -l")
    spec["aggregate"] = specs.parse_aggregation(args.aggregate)
    specs.agg_columns(spec["variables"], spec["aggregate"])  # check it now
    if args.time_interval:
        if not (args.time_start and args.time_end):
            raise SystemExit("A time series (-ti) needs -t0 and -t1")
        spec["windows"] = specs.time_windows(
            args.time_start, args.time_end, args.time_interval, args.time_units)
    plan_and_run(args, spec)


def cmd_from_img(args):
    from gtagg import sample

    sample.parse_window_specs(args.window)  # check it now
    spec = base_spec(args, "from_img")
    # Partition on the database's h3_03 cells, the cheapest to filter on.
    spec["grid"], spec["level"] = "h3", 3
    spec["image"] = {
        "path": args.image, "bands": args.bands, "band_names": args.band_names,
        "window": args.window, "fillna": args.fillna, "dropna": args.dropna, "geo": args.geo,
    }
    if args.region is None:
        args.region = image_bbox(args.image)
    plan_and_run(args, spec)


def image_bbox(image):
    import rasterio
    from rasterio.warp import transform_bounds

    from gtagg import sample

    with sample.raster_env(), rasterio.open(image) as src:
        w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    return f"{w},{s},{e},{n}"


def cmd_run(args):
    from gtagg import dps, run

    parts = dps.job_partitions(args.dataset, args.tag, args.job) if args.tag else None
    run.run(args.dataset.rstrip("/"), parts, threads=args.threads)


def cmd_submit(args):
    from gtagg import dps

    dps.submit(args.dataset.rstrip("/"), args.n_jobs, args.algo_version, args.queue)


def cmd_status(args):
    from gtagg import dps

    dps.status(args.dataset.rstrip("/"))


def cmd_rasterize(args):
    from gtagg import rasterize

    rasterize.rasterize(args.dataset.rstrip("/"), args.output, args.list,
                        args.merge, args.compress)


def cmd_merge(args):
    from gtiler.database import ducky

    from gtagg import store

    con = ducky.init_duckdb(temp_dir="/tmp/duckdb")
    con.sql(f"COPY (FROM '{store.join(args.dataset, 'parts')}/*.parquet') TO '{args.output}'")
    print(f"Wrote {args.output}")


def parser():
    p = argparse.ArgumentParser(prog="gtagg", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("aggregate", help="aggregate shots to EGI squares or H3 hexagons")
    add_shot_args(a)
    a.add_argument("-egi", "--egi", type=int, nargs="?", const=6, metavar="LEVEL",
                   help="EGI level 1-12 (6 = 1 km, as GEDI L4B) [bare flag: 6]")
    a.add_argument("-h3", "--h3-level", dest="h3_level", type=int, help="H3 level 0-15")
    a.add_argument("-l", "--list", nargs="+", required=True,
                   help="variables: database columns, or name=SQL, e.g. rh98=rh_l2a[99]")
    a.add_argument("-a", "--aggregate", default="mean",
                   help="'mean', \"['mean','std','p95']\", \"{'rh98':['mean','count']}\", "
                        "or a .json/.txt file of one [default: mean]. Functions: mean "
                        "median std var min max sum count nunique first last pNN")
    a.add_argument("-ti", "--time-interval", dest="time_interval", type=int, default=0,
                   help="time series: one output per window of this many units")
    a.add_argument("-tu", "--time-units", dest="time_units", default="years",
                   choices=["years", "months", "weeks", "days"])
    a.set_defaults(func=cmd_aggregate)

    f = sub.add_parser("from_img", help="sample raster values at GEDI shots")
    add_shot_args(f)
    f.add_argument("-i", "--image", required=True, help="raster file or VRT, local or s3://")
    f.add_argument("-B", "--bands", nargs="+", type=int, help="0-based bands [default: all]")
    f.add_argument("-b", "--band-names", dest="band_names", nargs="+",
                   help="names for the selected bands' columns")
    f.add_argument("-w", "--window", nargs="+",
                   help="window ops 'BZO': band B, odd size Z, op O (0 sum, 1 mean, "
                        "2 median, 3 mode, 4 std, 5 min, 6 max, 7 count, 8 range)")
    f.add_argument("-F", "--fillna", type=float, help="value for NoData pixels")
    f.add_argument("--dropna", action="store_true", help="drop shots with no band values")
    f.add_argument("-g", "--geo", action="store_true", help="include shot geometry")
    f.set_defaults(func=cmd_from_img)

    r = sub.add_parser("rasterize", help="GeoTIFFs of an aggregated dataset")
    r.add_argument("dataset")
    r.add_argument("-o", "--output", required=True, help="local directory for the GeoTIFFs")
    r.add_argument("-l", "--list", nargs="+", help="columns to rasterize [default: all]")
    r.add_argument("-m", "--merge", action="store_true", help="mosaic into one file per window")
    r.add_argument("--compress", default="LZW", choices=["LZW", "ZSTD", "DEFLATE", "NONE"])
    r.set_defaults(func=cmd_rasterize)

    s = sub.add_parser("status", help="how many partitions are finished")
    s.add_argument("dataset")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("submit", help="submit DPS jobs for the unfinished partitions")
    s.add_argument("dataset")
    s.add_argument("--n_jobs", type=int, required=True)
    add_dps_args(s)
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("run", help="compute unfinished partitions here (what DPS jobs run)")
    s.add_argument("dataset")
    s.add_argument("--tag", help="a submission's tag: run only its job --job's share")
    s.add_argument("--job", type=int)
    s.add_argument("--threads", type=int)
    s.set_defaults(func=cmd_run)

    m = sub.add_parser("merge", help="all of a dataset's partitions in one parquet file")
    m.add_argument("dataset")
    m.add_argument("-o", "--output", required=True)
    m.set_defaults(func=cmd_merge)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    args.func(args)
