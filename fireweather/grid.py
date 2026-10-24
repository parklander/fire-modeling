"""Regrid RDPS rotated lat-lon fields onto a Web Mercator display raster.

The mapping (display pixel -> native grid cell, nearest neighbour) is built once
from the lat/lon arrays in a GRIB message and cached to disk.
"""
import gzip
import math
import threading
import zlib

import eccodes
import numpy as np
from scipy.spatial import cKDTree

from . import config

_lock = threading.Lock()
_map = None  # dict with idx (H*W int32, -1 = outside), width, height, native shape


def _merc_y(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def _inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def raster_shape():
    b = config.BOUNDS
    w = config.RASTER_WIDTH
    dx = math.radians(b["east"] - b["west"]) / w
    h = int(round((_merc_y(b["north"]) - _merc_y(b["south"])) / dx))
    return h, w


def _build(handle):
    nj, ni = eccodes.codes_get(handle, "Nj"), eccodes.codes_get(handle, "Ni")
    lat = eccodes.codes_get_array(handle, "latitudes")
    lon = eccodes.codes_get_array(handle, "longitudes")
    dlat = eccodes.codes_get(handle, "jDirectionIncrementInDegrees")
    tree = cKDTree(_xyz(lat, lon))

    b = config.BOUNDS
    h, w = raster_shape()
    lons = b["west"] + (np.arange(w) + 0.5) * (b["east"] - b["west"]) / w
    ys = _merc_y(b["north"]) - (np.arange(h) + 0.5) * (_merc_y(b["north"]) - _merc_y(b["south"])) / h
    lats = _inv_merc_y(ys)
    glon, glat = np.meshgrid(lons, lats)
    dist, idx = tree.query(_xyz(glat.ravel(), glon.ravel()))
    # chord distance; anything beyond ~1.5 cells is outside the model domain
    idx = idx.astype(np.int32)
    idx[dist > np.radians(1.5 * dlat)] = -1
    np.savez_compressed(config.GRID_CACHE, idx=idx, height=h, width=w, nj=nj, ni=ni,
                        lat=lat.astype(np.float32), lon=lon.astype(np.float32),
                        bounds=np.array([b["west"], b["south"], b["east"], b["north"]]))


def get_map(handle=None):
    """Load the cached mapping, building it from `handle` if needed."""
    global _map
    with _lock:
        if _map is not None:
            return _map
        b = config.BOUNDS
        want = np.array([b["west"], b["south"], b["east"], b["north"]])
        ok = False
        if config.GRID_CACHE.exists():
            z = np.load(config.GRID_CACHE)
            ok = (np.allclose(z["bounds"], want) and int(z["width"]) == config.RASTER_WIDTH
                  and "lat" in z.files)
        if not ok:
            if handle is None:
                return None
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _build(handle)
            z = np.load(config.GRID_CACHE)
        _map = {k: (z[k] if k == "idx" else int(z[k])) for k in ("idx", "height", "width", "nj", "ni")}
        _map["lat"], _map["lon"] = z["lat"], z["lon"]
        return _map


_point_lookup = None


def point_pixel(lat, lon):
    """Display pixel that carries the model grid point nearest to (lat, lon).

    Hovering uses whichever display pixel is under the cursor, but for a point
    forecast we want the true nearest RDPS cell, which can differ in steep terrain.
    Returns (flat_pixel_index, cell_lat, cell_lon) or None if outside the display.
    """
    global _point_lookup
    m = get_map()
    if m is None:
        return None
    if _point_lookup is None:
        idx = m["idx"]
        inverse = np.full(m["nj"] * m["ni"], -1, dtype=np.int64)
        pix = np.flatnonzero(idx >= 0)
        cells, first = np.unique(idx[pix], return_index=True)
        inverse[cells] = pix[first]
        _point_lookup = (cKDTree(_xyz(m["lat"], m["lon"])), inverse)
    tree, inverse = _point_lookup
    _, k = tree.query(_xyz(np.array([lat]), np.array([lon]))[0])
    if inverse[k] >= 0:
        return int(inverse[k]), float(m["lat"][k]), float(m["lon"][k])
    px = latlon_to_pixel(lat, lon)  # cell skipped by the display raster (low latitudes)
    if px is None:
        return None
    return px[0] * m["width"] + px[1], float(m["lat"][k]), float(m["lon"][k])


def encode(values, var):
    """Quantise a display-raster float array to uint16 (0 = nodata) and byte-shuffle.

    Output layout is [all high bytes][all low bytes], which gzips much better than
    interleaved little-endian; the browser un-shuffles it.
    """
    spec = config.ALL_VARS[var]
    q = np.round((values - spec["offset"]) * spec["scale"]) + 1
    q = np.clip(np.nan_to_num(q, nan=1), 1, 65535).astype(np.uint16)
    q[np.isnan(values)] = 0
    return gzip.compress(np.concatenate([(q >> 8).astype(np.uint8), (q & 0xFF).astype(np.uint8)]).tobytes(), 6)


def decode(blob, var):
    spec = config.ALL_VARS[var]
    raw = np.frombuffer(gzip.decompress(blob), dtype=np.uint8)
    n = raw.size // 2
    q = (raw[:n].astype(np.uint16) << 8) | raw[n:]
    out = (q.astype(np.float32) - 1) / spec["scale"] + spec["offset"]
    out[q == 0] = np.nan
    return out


def decode_pixel(blob, var, idx):
    """Value of one pixel from an encoded frame, decompressing only as far as needed."""
    spec = config.ALL_VARS[var]
    n = int.from_bytes(blob[-4:], "little") // 2  # gzip trailer holds the uncompressed size
    raw = zlib.decompressobj(wbits=31).decompress(blob, n + idx + 1)
    q = (raw[idx] << 8) | raw[n + idx]
    return None if q == 0 else (q - 1) / spec["scale"] + spec["offset"]


def grib_to_raster(path, var):
    """Decode one GRIB2 file and return the display-raster float array (H, W)."""
    with open(path, "rb") as fh:
        h = eccodes.codes_grib_new_from_file(fh)
        try:
            m = get_map(h)
            vals = eccodes.codes_get_values(h)
            if eccodes.codes_get(h, "bitmapPresent"):
                fill = config.DOWNLOAD_VARS[var].get("missing_as", np.nan)
                vals = np.where(vals == eccodes.codes_get(h, "missingValue"), fill, vals)
        finally:
            eccodes.codes_release(h)
    conv = config.DOWNLOAD_VARS[var]["convert"]
    if conv:
        vals = conv(vals)
    flat = np.where(m["idx"] >= 0, vals[np.maximum(m["idx"], 0)], np.nan).astype(np.float32)
    return flat.reshape(m["height"], m["width"])


def pixel_latlon():
    """Flat (lat, lon) arrays of display-pixel centres."""
    b = config.BOUNDS
    h, w = raster_shape()
    lons = b["west"] + (np.arange(w) + 0.5) * (b["east"] - b["west"]) / w
    ys = _merc_y(b["north"]) - (np.arange(h) + 0.5) * (_merc_y(b["north"]) - _merc_y(b["south"])) / h
    glon, glat = np.meshgrid(lons, _inv_merc_y(ys))
    return glat.ravel(), glon.ravel()


def latlon_to_pixel(lat, lon):
    b = config.BOUNDS
    h, w = raster_shape()
    x = (lon - b["west"]) / (b["east"] - b["west"]) * w
    y = (_merc_y(b["north"]) - _merc_y(lat)) / (_merc_y(b["north"]) - _merc_y(b["south"])) * h
    if not (0 <= x < w and 0 <= y < h):
        return None
    return int(y), int(x)
