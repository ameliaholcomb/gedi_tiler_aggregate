"""The two output grids: EGI squares and H3 hexagons.

A grid gives, as DuckDB SQL over the tiled GEDI database:
  - `cell_sql()`: the id of the cell a shot falls in, at the target level, and
  - `in_partition(cell, pid)`: whether that cell is in coarse cell
    ("partition") `pid`.
Every target cell lies in exactly one partition, so a job that owns whole
partitions can compute any statistic (median, percentiles, ...) exactly,
with no merge step between jobs.

EGI (EASE Grid Index) is GEDI-H3's square grid on EASE-Grid 2.0
(EPSG:6933): levels 1-12, from ~1 m to ~160 km, with level 6 the GEDI L4B
1 km grid. Cell ids are the same uint64 hashes as GEDI-H3's (column
`egiNN`), and partitions are level 12 (~160 km) cells.

H3 cell ids are the standard H3 indexes (column `h3_NN`). Partitions are
level 3 cells (~12,400 km^2), which the database stores as `h3_03`, or the
target level itself when coarser than 3.
"""

import math

import h3
import numpy as np
import shapely
from pyproj import Transformer

# --- EGI, as defined by GEDI-H3 (gedih3/egi/config.py) ----------------------

EGI_CRS = "EPSG:6933"
EGI_RES6 = 1000.89502334956
EGI_RES = {
    1: round(EGI_RES6 / 1000, 6),
    2: round(EGI_RES6 / 200, 6),
    3: round(EGI_RES6 / 40, 6),
    4: round(EGI_RES6 / 10, 6),
    5: round(EGI_RES6 / 5, 6),
    6: round(EGI_RES6, 6),
    7: round(EGI_RES6 * 2, 6),
    8: round(EGI_RES6 * 10, 6),
    9: round(EGI_RES6 * 20, 6),
    10: round(EGI_RES6 * 40, 6),
    11: round(EGI_RES6 * 80, 6),
    12: round(EGI_RES6 * 160, 6),
}
EGI_OUTER = EGI_RES[12]
EGI_X0 = -17_367_530.445161499083042  # west edge of the grid, metres
EGI_Y0 = -7_314_540.830638599582016  # south edge of the grid, metres


def _floordiv(a, b):
    # Python and numpy's float floor division, which GEDI-H3 uses: it is
    # not floor(a / b) when a / b rounds up to an integer. DuckDB's `%` on
    # doubles is C's fmod; its fmod() function is not exact.
    return f"round((({a}) - (({a}) % {b})) / {b})"


def _egi_parts(level, x="ST_X(geometry_6933)", y="ST_Y(geometry_6933)"):
    """SQL for the outer (level 12) tile and inner pixel indices."""
    scale = EGI_RES[level]
    per_tile = round(EGI_OUTER / scale)
    parts = {}
    for axis, coord, origin in (("x", x, EGI_X0), ("y", y, EGI_Y0)):
        offset = f"(({coord}) - ({origin}))"
        outer = _floordiv(offset, EGI_OUTER)
        inner = _floordiv(f"({offset} % {EGI_OUTER})", scale)
        # At a tile edge, float error can put the pixel one past the tile's
        # last pixel: carry it into the next tile, as GEDI-H3 does.
        parts[f"{axis}_outer"] = f"({outer} + ({inner} >= {per_tile})::INT)"
        parts[f"{axis}_inner"] = f"(CASE WHEN {inner} >= {per_tile} THEN 0 ELSE {inner} END)"
    return parts


def egi_hash_sql(level):
    p = _egi_parts(level)
    return (
        f"({level}::UBIGINT * 1000000000000000000::UBIGINT"
        f" + {p['x_outer']}::UBIGINT * 1000000000000000::UBIGINT"
        f" + {p['y_outer']}::UBIGINT * 1000000000000::UBIGINT"
        f" + {p['x_inner']}::UBIGINT * 1000000::UBIGINT"
        f" + {p['y_inner']}::UBIGINT)"
    )


def egi_decode(ids):
    """(level, x_outer, y_outer, x_inner, y_inner) arrays from EGI hashes."""
    ids = np.asarray(ids, dtype=np.uint64)
    return (
        ids // np.uint64(10**18),
        ids % np.uint64(10**18) // np.uint64(10**15),
        ids % np.uint64(10**15) // np.uint64(10**12),
        ids % np.uint64(10**12) // np.uint64(10**6),
        ids % np.uint64(10**6),
    )


def egi_partition_id(x_outer, y_outer):
    return 12 * 10**18 + x_outer * 10**15 + y_outer * 10**12


_TO_EGI = Transformer.from_crs("OGC:CRS84", EGI_CRS, always_xy=True)


class EGI:
    kind = "egi"

    def __init__(self, level):
        if level not in EGI_RES:
            raise ValueError(f"EGI level must be 1-12, got {level}")
        self.level = level
        self.res = EGI_RES[level]
        self.per_tile = round(EGI_OUTER / self.res)
        self.cell_col = f"egi{level:02d}"
        self.partition_col = "egi12"
        self.id_type = "UBIGINT"

    def cell_sql(self):
        return egi_hash_sql(self.level)

    def in_partition(self, cell, pid):
        """SQL: is the cell `cell` in partition `pid` (GEDI-H3's to_parent)?
        Taken from the cell, not the shot, as at a tile edge a cell can be
        carried into the next tile. Compares only the tile digits: DuckDB
        1.5 fails on some UBIGINT arithmetic compared with a constant."""
        tile = int(pid) % 10**18 // 10**12
        return f"({cell}) % 1000000000000000000 // 1000000000000 = {tile}"

    def partitions_in_box(self, west, south, east, north):
        """Ids of the partitions overlapping a lon/lat box."""
        # EASE-Grid 2.0 is cylindrical, so a lon/lat box is a box in metres.
        # Pad it 1 km for cells carried across a partition edge.
        (x0, x1), (y0, y1) = _TO_EGI.transform([west, east], [south, north])
        x0, x1, y0, y1 = x0 - 1000, x1 + 1000, y0 - 1000, y1 + 1000
        xs = range(int((x0 - EGI_X0) // EGI_OUTER), int((x1 - EGI_X0) // EGI_OUTER) + 1)
        ys = range(int((y0 - EGI_Y0) // EGI_OUTER), int((y1 - EGI_Y0) // EGI_OUTER) + 1)
        return {egi_partition_id(x, y) for x in xs for y in ys}

    def raster_unit(self, partition_id):
        """Raster window of one partition: (crs, x0, y_top, res, width, height)."""
        _, xo, yo, _, _ = egi_decode([partition_id])
        x0 = EGI_X0 + int(xo[0]) * EGI_OUTER
        y_top = EGI_Y0 + (int(yo[0]) + 1) * EGI_OUTER
        return EGI_CRS, x0, y_top, self.res, self.per_tile, self.per_tile

    def pixel_index(self, cell_ids):
        """Global (col from west, row from north) of cells on this level's grid."""
        _, xo, yo, xi, yi = egi_decode(cell_ids)
        col = xo.astype(np.int64) * self.per_tile + xi.astype(np.int64)
        row_from_south = yo.astype(np.int64) * self.per_tile + yi.astype(np.int64)
        n_rows = round((-2 * EGI_Y0) / self.res)
        return col, n_rows - 1 - row_from_south

    def global_grid(self):
        """(crs, x0, y_top, res) of this level's grid over the whole globe.
        Pixel edges drift from GEDI-H3's by under 1 mm per 160 km tile."""
        return EGI_CRS, EGI_X0, -EGI_Y0, self.res


# --- H3 -----------------------------------------------------------------------

class H3:
    kind = "h3"

    def __init__(self, level):
        if not 0 <= level <= 15:
            raise ValueError(f"H3 level must be 0-15, got {level}")
        self.level = level
        self.partition_level = min(level, 3)
        self.cell_col = f"h3_{level:02d}"
        self.partition_col = f"h3_{self.partition_level:02d}"
        self.id_type = "BIGINT"
        # As GEDI-H3: pixels of twice the mean edge length, here in degrees
        # at the equator, on a global EPSG:4326 grid.
        self.res = 2 * h3.average_hexagon_edge_length(level, "m") / 111_320

    def cell_sql(self):
        if self.level == 12:
            return "h3_12"
        if self.level < 12:
            return f"h3_cell_to_parent(h3_12, {self.level})"
        return f"h3_latlng_to_cell(lat_lowestmode, lon_lowestmode, {self.level})"

    def in_partition(self, cell, pid):
        """SQL: is the cell `cell` in partition `pid`?"""
        if self.level == self.partition_level:
            return f"{cell} = {pid}"
        return f"h3_cell_to_parent({cell}, {self.partition_level}) = {pid}"

    def partitions_in_box(self, west, south, east, north):
        # Pad the box: H3 treats polygon edges as straight in lon/lat,
        # its cells' edges are not quite, and an extra partition costs little.
        pad = 0.05
        box = h3.LatLngPoly([
            (south - pad, west - pad), (south - pad, east + pad),
            (north + pad, east + pad), (north + pad, west - pad),
        ])
        cells = h3.h3shape_to_cells_experimental(box, self.partition_level, contain="overlap")
        return {h3.str_to_int(c) for c in cells}

    def global_grid(self):
        return "EPSG:4326", -180.0, 90.0, self.res


def make_grid(kind, level):
    return {"egi": EGI, "h3": H3}[kind](level)


# --- 1 x 1 degree database tiles ---------------------------------------------

def tile_box(tile_id):
    """(west, south, east, north) of a tile id like N07_W063 (its top left corner)."""
    lat = int(tile_id[1:3]) * (1 if tile_id[0] == "N" else -1)
    lon = int(tile_id[5:8]) * (1 if tile_id[4] == "E" else -1)
    return lon, lat - 1, lon + 1, lat


def tiles_union(tile_ids):
    return shapely.union_all([shapely.box(*tile_box(t)) for t in tile_ids])


def raster_pixels_in_box(grid, west, south, east, north):
    """Global pixel (col, row) ranges of an EPSG:4326 grid inside a lon/lat box."""
    _, x0, y_top, res = grid.global_grid()
    c0, c1 = math.floor((west - x0) / res), math.ceil((east - x0) / res)
    r0, r1 = math.floor((y_top - north) / res), math.ceil((y_top - south) / res)
    return c0, c1, r0, r1
