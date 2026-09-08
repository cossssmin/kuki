#!/usr/bin/env python3
"""Fetch and parse the public Copernicus CAMS WMS capabilities into a compact
JSON cache the shell can read, and persist the plugin's view state.

The WMS GetCapabilities document is ~600 KB of namespaced XML; parsing that in
QML on every open would be wasteful, so this helper distills it to just the
CAMS composition_* layers (name, title, group, default time, raw time
dimension, style names) and writes them once, refreshing only when stale.

It also proxies the OpenStreetMap basemap tiles (`tiles` command): QML's Image
cannot set a User-Agent, and OSM's tile policy rejects the generic Qt one, so
tiles are fetched here with a proper identity and cached on disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import pwd
import re
import secrets
import signal
import stat
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

WMS_BASE = "https://eccharts.ecmwf.int/wms/?token=public"
GET_CAPABILITIES = (
    WMS_BASE + "&service=WMS&version=1.3.0&request=GetCapabilities"
)
CACHE_MAX_AGE = 6 * 60 * 60  # seconds; refresh capabilities if older than this

# Every remote body is read through fetch_bytes() with a ceiling (cap + 1 so an
# overflow is detected, not truncated), no redirects, no environment proxies.
# One-shot commands also get an absolute wall-clock deadline (SIGALRM), which
# covers DNS and a trickling peer, not just per-operation socket timeouts.
CAPS_MAX_BYTES = 4 * 1024 * 1024
PROBE_MAX_BYTES = 64 * 1024
LEGEND_MAX_BYTES = 1024 * 1024
STATE_MAX_BYTES = 64 * 1024
COMMAND_DEADLINE = 60  # seconds
MAX_LAYERS = 500
MAX_STYLES = 32
MAX_TEXT = 200
MAX_TIME_DIMENSION = 16 * 1024
DEFAULT_LAYER = "composition_europe_pm2p5_forecast_surface"  # Europe first-run: Air Quality / PM2.5
DEFAULT_LAYER_GLOBAL = "composition_pm2p5"  # outside Europe: coarser global PM2.5
DEFAULT_STYLE = ""  # empty = the layer's own WMS default, always valid

# CAMS runs a high-res regional ensemble over Europe (composition_europe_*) and a
# coarser global model everywhere (composition_*). Inside this box we curate the
# Europe layers and the Europe-only pollen (Allergens); outside it the Air quality
# tab falls back to the global layers and Allergens is disabled.
EUROPE_BBOX = {"lat_min": 30.0, "lat_max": 72.0, "lon_min": -25.0, "lon_max": 45.0}

# OSM tile usage policy: identify the app, cache, and keep it to 2 connections.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_USER_AGENT = "Kuki/0.3.0 (Omarchy plugin; +https://github.com/cossssmin/kuki)"
TILE_MAX_AGE = 7 * 24 * 60 * 60  # seconds; re-download a cached tile after this
TILE_MAX_BYTES = 512 * 1024
TILE_MAX_ZOOM = 19
TILE_WORKERS = 2
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

USER_AGENT = "kuki"


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """The WMS legend endpoint answers with a 302 to a rendered PNG on the same
    host. Follow only https redirects that stay on the exact same host, a few
    hops at most; anything else is an error rather than a new destination."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        target = urllib.parse.urlsplit(newurl)
        origin = urllib.parse.urlsplit(req.full_url)
        if target.scheme != "https" or target.netloc != origin.netloc:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SameHostRedirect, urllib.request.ProxyHandler({}))


def fetch_bytes(url: str, limit: int, timeout: float, user_agent: str = USER_AGENT) -> bytes:
    """GET `url` and return at most `limit` bytes; anything beyond is an error."""
    if not url.startswith("https://"):
        raise ValueError("https only")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with OPENER.open(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response exceeds limit")
    return data


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


def plain(value: Any, limit: int = MAX_TEXT) -> str:
    """Untrusted text headed for a QML sink: drop markup characters and
    control/bidi characters, cap the length."""
    text = value if isinstance(value, str) else ""
    text = _CONTROL.sub("", text).replace("<", "").replace(">", "").replace("&", "")
    return text[:limit]


# --- Plugin-owned files ------------------------------------------------------
# Every file the plugin touches lives under a directory chain walked with held
# descriptors from the passwd home (not $HOME, which a same-UID child can set):
# each component opened O_DIRECTORY|O_NOFOLLOW, fstat-checked as ours, missing
# ones created; the plugin's own leaf directories are forced to 0700. Reads and
# writes are then relative to that descriptor, so nothing re-resolves a path.
CONFIG_PARTS = (".config", "omarchy", "kuki")
CACHE_PARTS = (".cache", "kuki", "tiles")
CAPS_FILE = "caps.json"
STATE_FILE = "state.json"
_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def home_dir() -> str:
    return pwd.getpwuid(os.geteuid()).pw_dir


def open_dir_chain(parts: tuple[str, ...], private_from: int) -> int:
    """Return a descriptor for home/<parts...>. Components from index
    `private_from` on belong to the plugin and are kept 0700; the XDG parents
    above are only required to be real directories we own."""
    if not parts or not all(_COMPONENT.fullmatch(p) and p not in (".", "..") for p in parts):
        raise PermissionError("refusing directory chain")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # The home itself may legitimately be a symlink, so it is not O_NOFOLLOW.
    fd = os.open(home_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, name in enumerate(parts):
            try:
                nfd = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = nfd
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError(f"untrusted directory component {name}")
            if index >= private_from and info.st_mode & 0o077:
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def config_dir() -> int:
    return open_dir_chain(CONFIG_PARTS, private_from=2)


def read_bounded(dirfd: int, name: str, limit: int) -> bytes | None:
    """Read `name` relative to `dirfd` through one descriptor: no symlink, no
    FIFO, regular file owned by us, at most `limit` bytes. None when absent or
    rejected (the caller falls back to defaults, it never repairs)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dirfd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > limit):
            return None
        os.set_blocking(fd, True)
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        return data if len(data) <= limit else None
    finally:
        os.close(fd)


def read_json(dirfd: int, name: str, fallback: Any, limit: int) -> Any:
    raw = read_bounded(dirfd, name, limit)
    if raw is None:
        return fallback
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def write_atomic(dirfd: int, name: str, data: bytes) -> None:
    """Exclusive random temporary next to the target, 0600 from creation,
    written through its descriptor, fsynced, renamed over the target (rename
    replaces a planted symlink instead of writing through it), directory
    fsynced. All relative to the held directory descriptor."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def write_json(dirfd: int, name: str, value: Any) -> None:
    write_atomic(dirfd, name, (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# Curated categories the widget surfaces up front. Everything not matched here
# falls into tier "advanced" (global upper-air gases, speciated PM/chemistry),
# reachable only through the ⚙ all-layers search.
POLLEN_SPECIES = {
    "alder": "Alder", "birch": "Birch", "grass": "Grass",
    "mugwort": "Mugwort", "olive": "Olive", "ragw": "Ragweed",
}
AIR_QUALITY = {
    "pm2p5": "PM2.5", "pm10": "PM10", "o3": "Ozone",
    "no2": "NO₂", "so2": "SO₂", "co": "CO",
}
AEROSOLS = {
    "aod550": "Total AOD", "duaod550": "Dust", "bbaod550": "Wildfire smoke",
    "ssaod550": "Sea salt", "suaod550": "Sulphate",
}
UV = {
    "uvindex": "UV now", "uvindex_daily_max": "UV daily max",
    "uvindex_clearsky": "UV clear-sky", "uvindex_clearsky_daily_max": "UV clear-sky max",
}
# Global surface air-quality layers, used outside Europe. Species keys match the
# Europe layers' so the per-category checklist carries across regions.
GLOBAL_AIR_QUALITY = {
    "pm2p5": ("PM2.5", "pm2p5"), "pm10": ("PM10", "pm10"),
    "o3_surface": ("Ozone", "o3"), "no2_surface": ("NO₂", "no2"),
    "so2_surface": ("SO₂", "so2"), "co_surface": ("CO", "co"),
}


def classify(name: str) -> dict[str, Any]:
    """Tag a composition_* layer with its curated category, tier, display name,
    and (for allergens) which variant/species it is. `tier` is 'curated' for the
    ~25 widget-worthy layers and 'advanced' for the long tail."""
    stem = name[len("composition_"):] if name.startswith("composition_") else name

    # Allergens: europe_pol_<species>_forecast_surface[_eea]
    if stem.startswith("europe_pol_"):
        rest = stem[len("europe_pol_"):]
        is_index = rest.endswith("_eea")
        core = rest[:-len("_eea")] if is_index else rest
        species = core[:-len("_forecast_surface")] if core.endswith("_forecast_surface") else core
        return {
            "category": "allergens", "tier": "curated", "region": "europe",
            "short": POLLEN_SPECIES.get(species, species.title()),
            "species": species, "variant": "index" if is_index else "concentration",
        }

    # Air quality (Europe): europe_<pollutant>_forecast_surface (analysis variants stay advanced)
    if stem.startswith("europe_") and stem.endswith("_forecast_surface"):
        pollutant = stem[len("europe_"):-len("_forecast_surface")]
        if pollutant in AIR_QUALITY:
            return {"category": "air-quality", "tier": "curated", "region": "europe",
                    "short": AIR_QUALITY[pollutant], "species": pollutant, "variant": "forecast"}

    # Air quality (global): coarser composition_* surface layers, used outside Europe.
    if stem in GLOBAL_AIR_QUALITY:
        short, species = GLOBAL_AIR_QUALITY[stem]
        return {"category": "air-quality", "tier": "curated", "region": "global",
                "short": short, "species": species, "variant": "forecast"}

    if stem in AEROSOLS:
        return {"category": "aerosols", "tier": "curated", "region": "any",
                "short": AEROSOLS[stem], "species": stem, "variant": "forecast"}

    if stem in UV:
        return {"category": "uv", "tier": "curated", "region": "any",
                "short": UV[stem], "species": stem, "variant": "forecast"}

    # Long tail: global gases at levels + speciated PM/chemistry + fire + analysis
    # grids. Reachable only through the ⚙ all-layers search ("Other").
    group = "gases" if any(g in stem for g in ("co2", "ch4", "co", "o3", "no2", "so2", "hcho")) else "surface"
    short = "Fire radiative power" if stem == "fire" else stem
    return {"category": "advanced", "tier": "advanced", "region": "any",
            "short": short, "species": stem, "variant": group}


LAYER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,120}")
TIME_DIMENSION = re.compile(r"[0-9TZ:,/PDHM.-]*")


def parse_capabilities(xml_bytes: bytes) -> list[dict[str, Any]]:
    """The WMS document is remote input: no DOCTYPE (entity expansion), a
    layer/style count ceiling, identifiers validated against a closed grammar,
    and free text (titles) stripped of markup before it can reach a sink."""
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise ValueError("DOCTYPE not allowed")
    root = ET.fromstring(xml_bytes)
    layers: list[dict[str, Any]] = []
    for element in root.iter():
        if local_name(element.tag) != "Layer":
            continue
        name_el = next((c for c in element if local_name(c.tag) == "Name"), None)
        if name_el is None or not name_el.text:
            continue
        name = name_el.text.strip()
        if not name.startswith("composition_") or not LAYER_NAME.fullmatch(name):
            continue
        title_el = next((c for c in element if local_name(c.tag) == "Title"), None)
        dim_el = next(
            (c for c in element if local_name(c.tag) == "Dimension" and c.get("name") == "time"),
            None,
        )
        styles = [
            s.text.strip()
            for style in element
            if local_name(style.tag) == "Style"
            for s in style
            if local_name(s.tag) == "Name" and s.text and LAYER_NAME.fullmatch(s.text.strip())
        ][:MAX_STYLES]
        time_dimension = dim_el.text.strip() if dim_el is not None and dim_el.text else ""
        if len(time_dimension) > MAX_TIME_DIMENSION or not TIME_DIMENSION.fullmatch(time_dimension):
            time_dimension = ""
        default_time = dim_el.get("default", "") if dim_el is not None else ""
        if not TIME_DIMENSION.fullmatch(default_time or ""):
            default_time = ""
        info = classify(name)
        info["short"] = plain(info["short"], 60)
        layers.append({
            "name": name,
            "title": plain(title_el.text.strip() if title_el is not None and title_el.text else name),
            "default": default_time or None,
            "time": time_dimension or None,
            "styles": styles,
            **info,
        })
        if len(layers) >= MAX_LAYERS:
            break
    return layers


def fetch_capabilities() -> bytes:
    return fetch_bytes(GET_CAPABILITIES, CAPS_MAX_BYTES, timeout=30)


def write_capabilities() -> dict[str, Any]:
    layers = parse_capabilities(fetch_capabilities())
    cache = {"generatedAt": int(time.time()), "layerCount": len(layers), "layers": layers}
    dirfd = config_dir()
    try:
        write_json(dirfd, CAPS_FILE, cache)
    finally:
        os.close(dirfd)
    return cache


def read_caps(fallback: Any) -> Any:
    dirfd = config_dir()
    try:
        return read_json(dirfd, CAPS_FILE, fallback, CAPS_MAX_BYTES)
    finally:
        os.close(dirfd)


def cache_is_stale() -> bool:
    cache = read_caps(None)
    if not isinstance(cache, dict) or not isinstance(cache.get("generatedAt"), (int, float)):
        return True
    return (time.time() - float(cache["generatedAt"])) > CACHE_MAX_AGE


def system_timezone() -> str:
    try:
        return Path("/etc/localtime").resolve().as_posix().split("zoneinfo/")[-1]
    except OSError:
        return os.environ.get("TZ", "")


def parse_iso6709(value: str) -> tuple[float, float] | None:
    """Parse a zone.tab coordinate like '+4426+02606' or '+404251+0743706'
    (±DDMM[SS]±DDDMM[SS]) into decimal (lat, lon)."""
    import re

    match = re.match(r"([+-]\d+)([+-]\d+)$", value.strip())
    if not match:
        return None

    def decimal(token: str, degree_digits: int) -> float:
        sign = -1 if token[0] == "-" else 1
        digits = token[1:]
        degrees = int(digits[:degree_digits])
        rest = digits[degree_digits:]
        minutes = int(rest[:2]) if len(rest) >= 2 else 0
        seconds = int(rest[2:4]) if len(rest) >= 4 else 0
        return sign * (degrees + minutes / 60 + seconds / 3600)

    return decimal(match.group(1), 2), decimal(match.group(2), 3)


def timezone_center() -> dict[str, float] | None:
    """The user's country/region centre, from the system timezone's coordinates
    in the tz database. Fully offline, no IP geolocation."""
    tz = system_timezone()
    if not tz:
        return None
    try:
        lines = Path("/usr/share/zoneinfo/zone1970.tab").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2] == tz:
            coords = parse_iso6709(parts[1])
            if coords:
                return {"lat": round(coords[0], 4), "lon": round(coords[1], 4)}
    return None


def user_region() -> str:
    """"europe" when the timezone centre falls in the CAMS regional domain, else
    "global". Unresolved timezone → "europe" (matches the Europe-wide fallback
    frame). Drives which air-quality layers are curated and whether Allergens is
    available."""
    center = timezone_center()
    if not center:
        return "europe"
    b = EUROPE_BBOX
    inside = (b["lat_min"] <= center["lat"] <= b["lat_max"]
              and b["lon_min"] <= center["lon"] <= b["lon_max"])
    return "europe" if inside else "global"


def default_state() -> dict[str, Any]:
    # Open on the user's country (from the system timezone), falling back to a
    # Europe-wide frame when the timezone can't be resolved.
    center = timezone_center() or {"lat": 49.0, "lon": 15.0}
    region = user_region()
    layer = DEFAULT_LAYER if region == "europe" else DEFAULT_LAYER_GLOBAL
    return {
        "layer": layer,
        "style": DEFAULT_STYLE,
        "home": dict(center),  # fixed location for the bar readout/alerts
        "barMetric": layer,  # which layer the bar indicator tracks
        "region": region,  # "europe" | "global"; picks curated layers + Allergens
        "center": center,
        "zoom": 6,  # tighter on the timezone's city, region still visible
        "timeIndex": -1,
        "overlayOpacity": 0.6,
        "enabledSpecies": {},  # per-category layer checklist (empty = all shown)
        "lastLayer": {},  # last-viewed layer per category, for chip switching
        "custom": False,  # the "Custom" search tab is active
    }


def current_state() -> dict[str, Any]:
    dirfd = config_dir()
    try:
        stored = read_json(dirfd, STATE_FILE, {}, STATE_MAX_BYTES)
    finally:
        os.close(dirfd)
    state = default_state()
    state.update(sanitize_state(stored))
    return state


def save_state(state: dict[str, Any]) -> None:
    dirfd = config_dir()
    try:
        write_json(dirfd, STATE_FILE, state)
    finally:
        os.close(dirfd)


def initialize(force: bool = False) -> dict[str, Any]:
    state = current_state()
    save_state(state)
    refreshed = False
    if force or cache_is_stale():
        try:
            write_capabilities()
            refreshed = True
        except Exception:  # network/parse failure must not break init
            state["capsError"] = "capabilities refresh failed"
    cache = read_caps({})
    count = cache.get("layerCount", 0) if isinstance(cache, dict) else 0
    return {**state, "layerCount": count, "capsRefreshed": refreshed}


def probe_value(layer: str, style: str, lat: float, lon: float, time: str) -> dict[str, Any]:
    """GetFeatureInfo at a lat/lon: build a small EPSG:3857 box around the point
    and query its centre pixel. Returns {"value": float|None, "unit": str}."""
    import math

    R = 6378137.0
    x = math.radians(lon) * R
    y = math.log(math.tan(math.pi / 4 + math.radians(max(-85.0, min(85.0, lat))) / 2)) * R
    d = 20000.0  # ~20 km half-box
    params = [
        "service=WMS", "version=1.3.0", "request=GetFeatureInfo",
        "layers=" + urllib.parse.quote(layer),
        "query_layers=" + urllib.parse.quote(layer),
        "styles=" + urllib.parse.quote(style or ""),
        "crs=EPSG:3857",
        "bbox=" + ",".join(str(v) for v in (x - d, y - d, x + d, y + d)),
        "width=100", "height=100", "i=50", "j=50",
        "info_format=text/plain",
    ]
    if time:
        params.append("dim_time=" + urllib.parse.quote(time))
    url = WMS_BASE + "&" + "&".join(params)
    try:
        text = fetch_bytes(url, PROBE_MAX_BYTES, timeout=15).decode("utf-8", "replace")
        match = re.search(r"Value:\s*([-\d.eE]+)\s*(\S+)?", text)
        if not match:
            return {"value": None, "unit": ""}
        value = float(match.group(1))
    except Exception:
        return {"value": None, "unit": ""}
    if not math.isfinite(value):
        return {"value": None, "unit": ""}
    unit = plain(match.group(2) or "", 16)
    if unit == "default":
        unit = ""
    return {"value": value, "unit": unit}


def legend_url(layer: str, style: str) -> str:
    return WMS_BASE + "&" + "&".join([
        "request=GetLegend",
        "layers=" + urllib.parse.quote(layer),
        "styles=" + urllib.parse.quote(style or ""),
        "width=350", "height=50", "format=image/png",
    ])


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Minimal decoder for 8-bit, non-interlaced RGB/RGBA PNGs (all the WMS
    legends are). Returns (width, height, channels, raw_pixels)."""
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG")
    i, width, height, color_type, idat = 8, 0, 0, 0, b""
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        chunk = data[i + 8:i + 8 + length]
        i += 12 + length
        if kind == b"IHDR":
            width, height, _bit, color_type = struct.unpack(">IIBB", chunk[:10])
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    # Legends are ~350x50; refuse anything that would decompress into a
    # large buffer (the declared size bounds the inflate, not the other way).
    if not (0 < width <= 2048 and 0 < height <= 512):
        raise ValueError("legend dimensions out of range")
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    expected = (stride + 1) * height
    raw = zlib.decompressobj().decompress(idat, expected + 1)
    if len(raw) != expected:
        raise ValueError("legend pixel data size mismatch")
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out += line
        prev = line
    return width, height, channels, bytes(out)


def legend_colors(layer: str, style: str) -> list[str]:
    """Discrete band colours of a layer/style legend, low→high, as hex. Samples
    the colour bar's mid row, coalesces runs, and drops the white bookends."""
    data = fetch_bytes(legend_url(layer, style), LEGEND_MAX_BYTES, timeout=20)
    width, height, channels, buf = decode_png(data)
    y = height // 2

    def pixel(x: int) -> tuple[int, int, int]:
        o = (y * width + x) * channels
        return buf[o], buf[o + 1], buf[o + 2]

    runs: list[tuple[tuple[int, int, int], int]] = []
    current: tuple[int, int, int] | None = None
    count = 0
    for x in range(2, width - 2):
        c = pixel(x)
        if current is None or sum(abs(c[k] - current[k]) for k in range(3)) > 24:
            if current is not None:
                runs.append((current, count))
            current, count = c, 1
        else:
            count += 1
    if current is not None:
        runs.append((current, count))

    colors = []
    for (r, g, b), n in runs:
        if n < 6:
            continue
        if r > 245 and g > 245 and b > 245:  # white border/background
            continue
        colors.append("#%02x%02x%02x" % (r, g, b))
    return colors[:64]


def valid_tile_key(key: str) -> bool:
    match = re.fullmatch(r"(\d{1,2})/(\d{1,7})/(\d{1,7})", key)
    if not match:
        return False
    z, x, y = (int(v) for v in match.groups())
    return z <= TILE_MAX_ZOOM and x < 2 ** z and y < 2 ** z


def tile_dir(key: str) -> tuple[int, str]:
    """Held descriptor for ~/.cache/kuki/tiles/<z>/<x> plus the file name."""
    z, x, y = key.split("/")
    return open_dir_chain(CACHE_PARTS + (z, x), private_from=1), f"{y}.png"


def cached_tile(dirfd: int, name: str) -> bytes | None:
    """The cached PNG, read through its own descriptor, if it is a regular
    file we own, well-formed, and younger than TILE_MAX_AGE. A planted symlink
    or a stale file is simply not served; the next download replaces it."""
    try:
        info = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except OSError:
        return None
    if time.time() - info.st_mtime >= TILE_MAX_AGE:
        return None
    data = read_bounded(dirfd, name, TILE_MAX_BYTES)
    return data if data and data[:8] == PNG_MAGIC else None


def download_tile(key: str) -> bytes:
    z, x, y = key.split("/")
    data = fetch_bytes(TILE_URL.format(z=z, x=x, y=y), TILE_MAX_BYTES,
                       timeout=20, user_agent=TILE_USER_AGENT)
    if data[:8] != PNG_MAGIC:
        raise ValueError("tile is not a PNG")
    return data


def serve_tiles() -> None:
    """Read `z/x/y` keys from stdin, one per line; answer `z/x/y data:image/png;
    base64,...` with the validated bytes themselves (the shell never opens a
    path), or `z/x/y !` if the tile could not be fetched. Cached tiles answer
    immediately; the rest go through a small worker pool. Input lines are
    bounded at 64 bytes: a valid key is at most 19."""
    lock = threading.Lock()

    def reply(key: str, result: str) -> None:
        with lock:
            print(f"{key} {result}", flush=True)

    def data_url(data: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    def work(key: str) -> None:
        try:
            dirfd, name = tile_dir(key)
        except OSError:
            reply(key, "!")
            return
        try:
            data = cached_tile(dirfd, name)
            if data is None:
                data = download_tile(key)
                write_atomic(dirfd, name, data)
            reply(key, data_url(data))
        except Exception:
            reply(key, "!")
        finally:
            os.close(dirfd)

    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as pool:
        while True:
            line = sys.stdin.buffer.readline(64)
            if not line:
                break
            key = line.decode("ascii", "replace").strip()
            if valid_tile_key(key):
                pool.submit(work, key)


STATE_KEYS = {
    "layer": str, "style": str, "home": dict, "barMetric": str, "region": str,
    "center": dict, "zoom": int, "timeIndex": int, "overlayOpacity": float,
    "enabledSpecies": dict, "lastLayer": dict, "custom": bool,
}


def sanitize_state(value: Any) -> dict[str, Any]:
    """Keep only known keys with the expected shape and range; the shell
    applies the same rules on read, so a bad file can never pick a huge zoom
    or push markup into a label."""

    def point(v: Any) -> dict[str, float] | None:
        if not isinstance(v, dict):
            return None
        lat, lon = v.get("lat"), v.get("lon")
        if not all(isinstance(c, (int, float)) and math.isfinite(c) for c in (lat, lon)):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"lat": float(lat), "lon": float(lon)}

    def name(v: Any) -> str:
        return v if isinstance(v, str) and LAYER_NAME.fullmatch(v) else ""

    out: dict[str, Any] = {}
    if not isinstance(value, dict):
        return out
    for key in ("layer", "style", "barMetric"):
        if name(value.get(key)):
            out[key] = value[key]
    if value.get("style") == "":
        out["style"] = ""  # empty = the layer's own WMS default
    for key in ("home", "center"):
        p = point(value.get(key))
        if p:
            out[key] = p
    if value.get("region") in ("europe", "global"):
        out["region"] = value["region"]
    zoom = value.get("zoom")
    if isinstance(zoom, (int, float)) and math.isfinite(zoom):
        out["zoom"] = max(2, min(9, int(zoom)))
    idx = value.get("timeIndex")
    if isinstance(idx, (int, float)) and math.isfinite(idx):
        out["timeIndex"] = max(-1, min(10000, int(idx)))
    opacity = value.get("overlayOpacity")
    if isinstance(opacity, (int, float)) and math.isfinite(opacity):
        out["overlayOpacity"] = max(0.0, min(1.0, float(opacity)))
    if isinstance(value.get("custom"), bool):
        out["custom"] = value["custom"]
    species = value.get("enabledSpecies")
    if isinstance(species, dict):
        out["enabledSpecies"] = {
            k: [s for s in v if isinstance(s, str) and LAYER_NAME.fullmatch(s)][:64]
            for k, v in list(species.items())[:16]
            if isinstance(k, str) and LAYER_NAME.fullmatch(k) and isinstance(v, list)
        }
    last = value.get("lastLayer")
    if isinstance(last, dict):
        out["lastLayer"] = {
            k: v for k, v in list(last.items())[:16]
            if isinstance(k, str) and LAYER_NAME.fullmatch(k) and name(v)
        }
    return out


def write_state_from_stdin() -> int:
    """`state-write`: one JSON document on stdin (bounded), sanitized, then
    published atomically at 0600. Exit 3 on an oversized or malformed body."""
    payload = sys.stdin.buffer.read(STATE_MAX_BYTES + 1)
    if len(payload) > STATE_MAX_BYTES:
        return 3
    try:
        incoming = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 3
    state = default_state()
    state.update(sanitize_state(incoming))
    save_state(state)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--force", action="store_true")
    caps = commands.add_parser("capabilities")
    caps.add_argument("--force", action="store_true")
    commands.add_parser("state")
    read = commands.add_parser("read")
    read.add_argument("what", choices=["caps", "state"])
    legend = commands.add_parser("legend")
    legend.add_argument("--layer", required=True)
    legend.add_argument("--style", default="")
    probe = commands.add_parser("probe")
    probe.add_argument("--layer", required=True)
    probe.add_argument("--style", default="")
    probe.add_argument("--lat", type=float, required=True)
    probe.add_argument("--lon", type=float, required=True)
    probe.add_argument("--time", default="")
    commands.add_parser("tiles")
    commands.add_parser("state-write")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command != "tiles":
        # Absolute deadline for one-shot commands; SIGALRM's default action
        # terminates the interpreter wherever it is (DNS, a stalled read).
        signal.alarm(COMMAND_DEADLINE)
    if args.command == "init":
        print(json.dumps(initialize(args.force), separators=(",", ":")))
    elif args.command == "capabilities":
        if args.force or cache_is_stale():
            write_capabilities()
        cache = read_caps({})
        if not isinstance(cache, dict):
            cache = {}
        print(json.dumps({"layerCount": cache.get("layerCount", 0),
                          "generatedAt": cache.get("generatedAt", 0)},
                         separators=(",", ":")))
    elif args.command == "read":
        # The shell's only way to read its own files: bounded, descriptor-
        # validated, and (for state) merged over defaults and sanitized.
        if args.what == "state":
            print(json.dumps(current_state(), separators=(",", ":")))
        else:
            cache = read_caps({})
            print(json.dumps(cache if isinstance(cache, dict) else {}, separators=(",", ":")))
    elif args.command == "state":
        print(json.dumps(current_state(), separators=(",", ":")))
    elif args.command == "legend":
        try:
            colors = legend_colors(args.layer, args.style)
        except Exception:
            colors = []
        print(json.dumps({"layer": args.layer, "style": args.style, "colors": colors},
                         separators=(",", ":")))
    elif args.command == "probe":
        print(json.dumps(probe_value(args.layer, args.style, args.lat, args.lon, args.time),
                         separators=(",", ":")))
    elif args.command == "tiles":
        serve_tiles()
    elif args.command == "state-write":
        return write_state_from_stdin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
