"""Sample raster values at GEDI shots (GEDI-H3's gh3_from_img).

Each shot takes the value of the pixel it falls in. Window operations
(`-w BZO`: band B, odd window size Z, operation O) summarise the Z x Z
pixels around that pixel. Unlike GEDI-H3, which filters the whole raster
with scipy, they skip NoData pixels (and pixels off the raster's edge)
rather than propagating them.
"""

import os
import re
import warnings

import boto3
import numpy as np
import rasterio
from rasterio.session import AWSSession
from rasterio.windows import Window
from pyproj import Transformer

WINDOW_OPS = {0: "sum", 1: "mean", 2: "median", 3: "mode", 4: "std",
              5: "min", 6: "max", 7: "count", 8: "range"}


def parse_window_specs(specs):
    """[{band, size, op}] from 3-digit specs like '033' (GEDI-H3's -w)."""
    out = []
    for spec in specs or []:
        if not re.fullmatch(r"\d{3}", spec):
            raise ValueError(f"Window spec must be 3 digits (band/size/op), got {spec!r}")
        band, size, op = map(int, spec)
        if size % 2 == 0:
            raise ValueError(f"Window size must be odd, got {size} in {spec!r}")
        if op not in WINDOW_OPS:
            raise ValueError(f"Window op must be 0-8, got {op} in {spec!r}")
        out.append({"band": band, "size": size, "op": WINDOW_OPS[op]})
    return out


def raster_env():
    """GDAL settings to read rasters on S3 with this session's credentials."""
    return rasterio.Env(session=AWSSession(boto3.Session()), GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR")


def band_names(image, src, bands, names=None):
    """Column names of all the raster's bands, as GEDI-H3 names them: the
    band descriptions, else {image name}_bN; then `names`, if given, for
    the selected `bands`."""
    if all(src.descriptions):
        out = list(src.descriptions)
    else:
        prefix = re.sub(r"\s+", "_", os.path.splitext(os.path.basename(image.rstrip("/")))[0])
        out = [f"{prefix}_b{i}" for i in range(src.count)]
    if names:
        if len(names) != len(bands):
            raise ValueError(f"{len(names)} band names for {len(bands)} bands")
        for b, n in zip(bands, names):
            out[b] = n
    return out


def _mode(w):
    """Most common non-NaN value of each row (the smallest, on ties)."""
    s = np.sort(w, axis=1)  # NaNs sort last
    run = np.ones(s.shape, dtype=np.int16)
    for j in range(1, s.shape[1]):
        run[:, j] = np.where(s[:, j] == s[:, j - 1], run[:, j - 1] + 1, 1)
    run[np.isnan(s)] = 0
    out = s[np.arange(len(s)), run.argmax(axis=1)]
    out[run.max(axis=1) == 0] = np.nan
    return out


def _window_stat(w, op):
    # Windows of only NoData give NaN; numpy warns about each of them.
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        valid = np.isfinite(w)
        n = valid.sum(axis=1)
        if op == "count":
            return n.astype(float)
        if op == "mode":
            return _mode(w)
        stat = {
            "sum": np.nansum, "mean": np.nanmean, "median": np.nanmedian,
            "min": np.nanmin, "max": np.nanmax,
            "std": lambda a, axis: np.nanstd(a, axis=axis, ddof=0),
            "range": lambda a, axis: np.nanmax(a, axis=axis) - np.nanmin(a, axis=axis),
        }[op]
        out = stat(w, axis=1)
        out[n == 0] = np.nan  # nansum of nothing is 0, not missing
        return out


def sample(src, lon, lat, bands, names, window_ops, fillna=None, chunk=200_000):
    """{column: values} of the raster at the points.

    bands: 0-based indices of the raster bands to output.
    names: column names of all the raster's bands (see band_names).
    """
    to_ras = Transformer.from_crs("OGC:CRS84", src.crs, always_xy=True)
    x, y = to_ras.transform(lon, lat)
    inv = ~src.transform
    col, row = inv * (np.asarray(x), np.asarray(y))
    col, row = np.floor(col).astype(np.int64), np.floor(row).astype(np.int64)
    inside = (col >= 0) & (col < src.width) & (row >= 0) & (row < src.height)

    out = {names[b]: np.full(len(lon), np.nan) for b in bands}
    win_names = [f"{names[w['band']]}_{w['op']}_{w['size']}x{w['size']}" for w in window_ops]
    out.update({n: np.full(len(lon), np.nan) for n in win_names})
    if not inside.any():
        return out

    # Read the pixels around the points once, padded for the windows.
    pad = max([w["size"] // 2 for w in window_ops], default=0)
    r0, r1 = row[inside].min() - pad, row[inside].max() + pad + 1
    c0, c1 = col[inside].min() - pad, col[inside].max() + pad + 1
    needed = sorted(set(bands) | {w["band"] for w in window_ops})
    data = src.read(
        [b + 1 for b in needed],
        window=Window(c0, r0, c1 - c0, r1 - r0),
        boundless=True, masked=True, out_dtype="float64",
    ).filled(np.nan)
    if fillna is not None:
        data[np.isnan(data)] = fillna
    layer = {b: data[i] for i, b in enumerate(needed)}

    idx = np.flatnonzero(inside)
    rr, cc = row[idx] - r0, col[idx] - c0
    for b in bands:
        out[names[b]][idx] = layer[b][rr, cc]
    for w, n in zip(window_ops, win_names):
        h = w["size"] // 2
        offsets = np.arange(-h, h + 1)
        for s in range(0, len(idx), chunk):
            r = rr[s:s + chunk, None, None] + offsets[None, :, None]
            c = cc[s:s + chunk, None, None] + offsets[None, None, :]
            vals = layer[w["band"]][r, c].reshape(len(r), -1)
            out[n][idx[s:s + chunk]] = _window_stat(vals, w["op"])
    return out

