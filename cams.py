#!/usr/bin/env python3
"""Fetch and parse the public Copernicus CAMS WMS capabilities into a compact
JSON cache the shell can read, and persist the plugin's view state.

The WMS GetCapabilities document is ~600 KB of namespaced XML; parsing that in
QML on every open would be wasteful, so this helper distills it to just the
CAMS composition_* layers (name, title, group, default time, raw time
dimension, style names) and writes them once, refreshing only when stale.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

WMS_BASE = "https://eccharts.ecmwf.int/wms/?token=public"
GET_CAPABILITIES = (
    WMS_BASE + "&service=WMS&version=1.3.0&request=GetCapabilities"
)
CACHE_MAX_AGE = 6 * 60 * 60  # seconds; refresh capabilities if older than this
DEFAULT_LAYER = "composition_europe_pm2p5_forecast_surface"  # first-run: Air Quality / PM2.5
DEFAULT_STYLE = ""  # empty = the layer's own WMS default, always valid


def base_dir() -> Path:
    override = os.environ.get("KUKI_CONFIG_DIR")
    root = Path(override).expanduser() if override else Path.home() / ".config/omarchy/kuki"
    return root


def caps_path() -> Path:
    return base_dir() / "caps.json"


def state_path() -> Path:
    return base_dir() / "state.json"


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


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
            "category": "allergens", "tier": "curated",
            "short": POLLEN_SPECIES.get(species, species.title()),
            "species": species, "variant": "index" if is_index else "concentration",
        }

    # Air quality: europe_<pollutant>_forecast_surface (analysis variants stay advanced)
    if stem.startswith("europe_") and stem.endswith("_forecast_surface"):
        pollutant = stem[len("europe_"):-len("_forecast_surface")]
        if pollutant in AIR_QUALITY:
            return {"category": "air-quality", "tier": "curated",
                    "short": AIR_QUALITY[pollutant], "species": pollutant, "variant": "forecast"}

    if stem in AEROSOLS:
        return {"category": "aerosols", "tier": "curated", "short": AEROSOLS[stem],
                "species": stem, "variant": "forecast"}

    if stem in UV:
        return {"category": "uv", "tier": "curated", "short": UV[stem],
                "species": stem, "variant": "forecast"}

    if stem == "fire":
        return {"category": "fire", "tier": "curated", "short": "Fire radiative power",
                "species": "fire", "variant": "forecast"}

    # Long tail: global gases at levels + speciated PM/chemistry + analysis grids.
    group = "gases" if any(g in stem for g in ("co2", "ch4", "co", "o3", "no2", "so2", "hcho")) else "surface"
    return {"category": "advanced", "tier": "advanced", "short": stem,
            "species": stem, "variant": group}


def parse_capabilities(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    layers: list[dict[str, Any]] = []
    for element in root.iter():
        if local_name(element.tag) != "Layer":
            continue
        name_el = next((c for c in element if local_name(c.tag) == "Name"), None)
        if name_el is None or not name_el.text:
            continue
        name = name_el.text.strip()
        if not name.startswith("composition_"):
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
            if local_name(s.tag) == "Name" and s.text
        ]
        layers.append({
            "name": name,
            "title": (title_el.text.strip() if title_el is not None and title_el.text else name),
            "default": (dim_el.get("default") if dim_el is not None else None),
            "time": (dim_el.text.strip() if dim_el is not None and dim_el.text else None),
            "styles": styles,
            **classify(name),
        })
    return layers


def fetch_capabilities() -> str:
    request = urllib.request.Request(GET_CAPABILITIES, headers={"User-Agent": "kuki"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def write_capabilities() -> dict[str, Any]:
    layers = parse_capabilities(fetch_capabilities())
    cache = {"generatedAt": int(time.time()), "layerCount": len(layers), "layers": layers}
    atomic_write(caps_path(), cache)
    return cache


def cache_is_stale() -> bool:
    cache = read_json(caps_path(), None)
    if not isinstance(cache, dict) or "generatedAt" not in cache:
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


def default_state() -> dict[str, Any]:
    # Open on the user's country (from the system timezone), falling back to a
    # Europe-wide frame when the timezone can't be resolved.
    center = timezone_center() or {"lat": 49.0, "lon": 15.0}
    return {
        "layer": DEFAULT_LAYER,
        "style": DEFAULT_STYLE,
        "home": dict(center),  # fixed location for the bar readout/alerts
        "barMetric": DEFAULT_LAYER,  # which layer the bar indicator tracks
        "center": center,
        "zoom": 6,  # tighter on the timezone's city, region still visible
        "timeIndex": -1,
        "overlayOpacity": 0.6,
        "enabledSpecies": {},  # per-category layer checklist (empty = all shown)
        "lastLayer": {},  # last-viewed layer per category, for chip switching
        "custom": False,  # the "Custom" search tab is active
    }


def current_state() -> dict[str, Any]:
    stored = read_json(state_path(), {})
    state = default_state()
    if isinstance(stored, dict):
        state.update({k: v for k, v in stored.items() if k in state})
    return state


def initialize(force: bool = False) -> dict[str, Any]:
    state = current_state()
    atomic_write(state_path(), state)
    refreshed = False
    if force or cache_is_stale():
        try:
            write_capabilities()
            refreshed = True
        except Exception as error:  # network/parse failure must not break init
            state["capsError"] = str(error)
    cache = read_json(caps_path(), {"layerCount": 0})
    return {**state, "layerCount": cache.get("layerCount", 0), "capsRefreshed": refreshed}


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
        text = urllib.request.urlopen(url, timeout=15).read().decode("utf-8")
    except Exception:
        return {"value": None, "unit": ""}
    match = re.search(r"Value:\s*([-\d.eE]+)\s*(\S+)?", text)
    if not match:
        return {"value": None, "unit": ""}
    unit = match.group(2) or ""
    if unit == "default":
        unit = ""
    return {"value": float(match.group(1)), "unit": unit}


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
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i, width, height, color_type, idat = 8, 0, 0, 0, b""
    while i < len(data):
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
    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(idat)
    stride = width * channels
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
    data = urllib.request.urlopen(legend_url(layer, style), timeout=20).read()
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
    return colors


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--force", action="store_true")
    caps = commands.add_parser("capabilities")
    caps.add_argument("--force", action="store_true")
    commands.add_parser("state")
    commands.add_parser("caps-path")
    legend = commands.add_parser("legend")
    legend.add_argument("--layer", required=True)
    legend.add_argument("--style", default="")
    probe = commands.add_parser("probe")
    probe.add_argument("--layer", required=True)
    probe.add_argument("--style", default="")
    probe.add_argument("--lat", type=float, required=True)
    probe.add_argument("--lon", type=float, required=True)
    probe.add_argument("--time", default="")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "init":
        print(json.dumps(initialize(args.force), separators=(",", ":")))
    elif args.command == "capabilities":
        if args.force or cache_is_stale():
            write_capabilities()
        cache = read_json(caps_path(), {"layerCount": 0, "generatedAt": 0})
        print(json.dumps({"layerCount": cache.get("layerCount", 0),
                          "generatedAt": cache.get("generatedAt", 0)},
                         separators=(",", ":")))
    elif args.command == "state":
        print(json.dumps(current_state(), separators=(",", ":")))
    elif args.command == "caps-path":
        print(caps_path())
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
