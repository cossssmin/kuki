.pragma library

// Group keys → display labels for the layer picker (Phase 2 uses the picker;
// Phase 0/1 only needs the label lookup for the status line).
var groupLabels = {
  "air-quality": "Air quality",
  "allergens": "Allergens",
  "aerosols": "Aerosols",
  "uv": "UV",
  "gases": "Gases"
}

function parseCaps(raw) {
  var parsed = {}
  try { parsed = JSON.parse(String(raw || "{}")) }
  catch (error) { parsed = {} }
  return {
    generatedAt: Number(parsed.generatedAt) || 0,
    layerCount: Number(parsed.layerCount) || 0,
    layers: Array.isArray(parsed.layers) ? parsed.layers : []
  }
}

function parseState(raw) {
  var parsed = {}
  try { parsed = JSON.parse(String(raw || "{}")) }
  catch (error) { parsed = {} }
  return parsed
}

function findLayer(caps, name) {
  var layers = caps && caps.layers ? caps.layers : []
  for (var i = 0; i < layers.length; i++) {
    if (layers[i].name === name) return layers[i]
  }
  return null
}

// Parse an ISO8601 duration like "PT3H", "PT1H", "P1D" into milliseconds.
// CAMS only uses hour/day periods, so a small subset is enough.
function durationMs(period) {
  var match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$/.exec(String(period || ""))
  if (!match) return 0
  var days = Number(match[1] || 0)
  var hours = Number(match[2] || 0)
  var minutes = Number(match[3] || 0)
  return ((days * 24 + hours) * 60 + minutes) * 60 * 1000
}

// Expand a WMS time Dimension string into an array of ISO timestamps.
// The string is comma-separated tokens; each token is either a single instant
// or a "start/end/period" interval, e.g.
//   "2026-08-12T03:00:00Z,2026-08-12T06:00:00Z/2026-08-21T00:00:00Z/PT3H"
function expandTimes(dimension) {
  var out = []
  var tokens = String(dimension || "").split(",")
  for (var t = 0; t < tokens.length; t++) {
    var token = tokens[t].trim()
    if (!token) continue
    if (token.indexOf("/") === -1) {
      out.push(token)
      continue
    }
    var parts = token.split("/")
    if (parts.length < 3) { out.push(parts[0]); continue }
    var start = Date.parse(parts[0])
    var end = Date.parse(parts[1])
    var step = durationMs(parts[2])
    if (!isFinite(start) || !isFinite(end) || step <= 0) { out.push(parts[0]); continue }
    for (var ms = start; ms <= end; ms += step) {
      out.push(new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z"))
    }
  }
  return out
}

// Index of the forecast step closest to `nowMs` (defaults to the current time),
// so opening a layer lands on ~now rather than the analysis time.
function nearestTimeIndex(times, nowMs) {
  if (!times || times.length === 0) return 0
  var now = isFinite(nowMs) ? nowMs : Date.now()
  var best = 0, bestDiff = Infinity
  for (var i = 0; i < times.length; i++) {
    var diff = Math.abs(Date.parse(times[i]) - now)
    if (diff < bestDiff) { bestDiff = diff; best = i }
  }
  return best
}

// Short human label for a forecast valid time, in the local timezone.
function formatTime(iso) {
  var ms = Date.parse(iso)
  if (!isFinite(ms)) return String(iso || "")
  var d = new Date(ms)
  var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
  var hh = ("0" + d.getHours()).slice(-2)
  var mm = ("0" + d.getMinutes()).slice(-2)
  return days[d.getDay()] + " " + d.getDate() + " · " + hh + ":" + mm
}

function groupLabel(key) {
  return groupLabels[key] || key
}

// Drop the "(provided by CAMS, …)" attribution the WMS appends to every title.
function cleanTitle(title) {
  return String(title || "").replace(/\s*\(provided by[^)]*\)\s*$/i, "").trim()
}

// Legend end labels per category. All these layers measure a concentration or
// index where higher = more of the thing, but a bare "Low/High" is ambiguous
// (low air *quality* reads as bad, low *pollen* reads as good), so name what the
// ends actually mean. Higher is consistently the worse/heavier end.
var legendEndLabels = {
  "air-quality": { low: "Cleaner", high: "More polluted" },
  "allergens": { low: "Less pollen", high: "More pollen" },
  "aerosols": { low: "Clearer", high: "Hazier" },
  "uv": { low: "Low UV", high: "Extreme UV" }
}

// Qualitative health levels for the bar indicator. A single traffic-light ramp
// (green→purple) across all metrics, with per-metric numeric thresholds. Bounds
// are the upper edge of each of the first four levels; anything above is the
// fifth. Values are approximate, standard-ish (WHO UV, EEA-style air quality).
var LEVEL_NAMES = ["Low", "Moderate", "High", "Very high", "Extreme"]
var LEVEL_COLORS = ["#2ecc71", "#f1c40f", "#e67e22", "#e74c3c", "#8e44ad"]
var METRIC_BANDS = {
  "uv": [2, 5, 7, 10],
  "pm2p5": [10, 20, 40, 75],
  "pm10": [20, 40, 75, 150],
  "o3": [60, 120, 180, 240],
  "no2": [40, 90, 120, 230],
  "so2": [100, 200, 350, 500],
  "co": [4000, 8000, 12000, 16000],
  "pollen": [10, 30, 100, 300]
}

// Which band table applies to a layer (null = no health band, e.g. aerosols).
function metricKind(layer) {
  if (!layer) return null
  if (layer.category === "uv") return "uv"
  if (layer.category === "allergens") return "pollen"
  if (layer.category === "air-quality" && METRIC_BANDS[layer.species]) return layer.species
  return null
}

// Level {index, name, color} for a value of a given metric kind, or null.
function levelFor(kind, value) {
  var bounds = METRIC_BANDS[kind]
  if (!bounds || !isFinite(value)) return null
  var i = 0
  while (i < bounds.length && value > bounds[i]) i++
  return { index: i, name: LEVEL_NAMES[i], color: LEVEL_COLORS[i] }
}

function legendEnds(category) {
  return legendEndLabels[category] || { low: "Less", high: "More" }
}

// Curated-category ordering + the layer each category opens to (by `species`).
var CATEGORY_ORDER = ["air-quality", "allergens", "aerosols", "uv"]
var DEFAULT_SPECIES = {
  "air-quality": "pm2p5", "allergens": "grass",
  "aerosols": "aod550", "uv": "uvindex"
}

// CAMS's high-res regional ensemble covers this box (Europe); outside it the
// curated layers fall back to the coarser global model. Kept in sync with the
// EUROPE_BBOX in cams.py. `region` is view-driven: it tracks the map centre, so
// panning into North America (etc.) swaps the overlay to global data.
var EUROPE_BBOX = { latMin: 30.0, latMax: 72.0, lonMin: -25.0, lonMax: 45.0 }

function regionForPoint(lat, lon) {
  var b = EUROPE_BBOX
  var inside = lat >= b.latMin && lat <= b.latMax && lon >= b.lonMin && lon <= b.lonMax
  return inside ? "europe" : "global"
}

// The same curated metric in a different region: same category/species/variant,
// matching `region` (or region-agnostic). Used to swap e.g. the Europe PM2.5
// layer for the global one when the map pans out of Europe. Uses raw caps.
function regionEquivalent(caps, layer, region) {
  if (!layer) return null
  var layers = caps && caps.layers ? caps.layers : []
  for (var i = 0; i < layers.length; i++) {
    var l = layers[i]
    if (l.tier === "curated" && l.category === layer.category
        && l.species === layer.species
        && (l.region === region || l.region === "any")
        && (!layer.variant || l.variant === layer.variant))
      return l.name
  }
  return null
}

// A region-scoped view of caps: drops curated layers not valid for `region`
// ("europe"/"global"), keeping region-agnostic curated layers and the whole
// advanced long tail. Pass this to the category/picker helpers; keep raw caps
// for findLayer and the "Other" all-layers search.
function withRegion(caps, region) {
  if (!caps || !caps.layers) return caps
  var r = region || "europe"
  var layers = caps.layers.filter(function(l) {
    if (l.tier !== "curated") return true
    var lr = l.region || "any"
    return lr === "any" || lr === r
  })
  return { generatedAt: caps.generatedAt, layerCount: caps.layerCount, layers: layers }
}

function curatedLayers(caps) {
  var layers = caps && caps.layers ? caps.layers : []
  return layers.filter(function(l) { return l.tier === "curated" })
}

function layersInCategory(caps, category) {
  return curatedLayers(caps).filter(function(l) { return l.category === category })
}

// Categories that have curated layers for the scoped region, in display order.
// Allergens is always listed but flagged `disabled` where no pollen data exists
// (outside Europe), so the tab stays in place, muted and unclickable.
function availableCategories(caps) {
  var out = []
  for (var i = 0; i < CATEGORY_ORDER.length; i++) {
    var key = CATEGORY_ORDER[i]
    if (layersInCategory(caps, key).length > 0)
      out.push({ key: key, label: groupLabel(key) })
    else if (key === "allergens")
      out.push({ key: key, label: groupLabel(key), disabled: true })
  }
  return out
}

// The full member list for a category, one entry per layer (per species for
// allergens, deduped across the concentration/index variants so the toggle
// swaps `value` without changing the species set). Each carries a stable
// `species` key used for the per-category enable checklist.
function categoryMembers(caps, category, variant) {
  var members = layersInCategory(caps, category)
  if (category !== "allergens")
    return members.map(function(l) { return { value: l.name, label: l.short, species: l.species } })

  var wanted = variant || "index"
  var bySpecies = {}
  for (var i = 0; i < members.length; i++) {
    var l = members[i]
    if (!bySpecies[l.species]) bySpecies[l.species] = {}
    bySpecies[l.species][l.variant] = l
  }
  var out = []
  for (var s in bySpecies) {
    var pick = bySpecies[s][wanted] || bySpecies[s].index || bySpecies[s].concentration
    if (pick) out.push({ value: pick.name, label: pick.short, species: s })
  }
  return out
}

// Options for the category's layer picker, filtered to the enabled species.
// A null/empty `enabled` means all are enabled; a filter that removes every
// member falls back to all so the picker is never empty.
function categoryOptions(caps, category, variant, enabled) {
  var members = categoryMembers(caps, category, variant)
  var all = members.map(function(m) { return { value: m.value, label: m.label } })
  if (!enabled || enabled.length === 0) return all
  var set = {}
  for (var i = 0; i < enabled.length; i++) set[enabled[i]] = true
  var out = []
  for (var j = 0; j < members.length; j++)
    if (set[members[j].species]) out.push({ value: members[j].value, label: members[j].label })
  return out.length ? out : all
}

// Checklist options (value = stable species key) and the full species list.
function categorySpeciesOptions(caps, category, variant) {
  return categoryMembers(caps, category, variant).map(function(m) {
    return { value: m.species, label: m.label }
  })
}

function allSpecies(caps, category, variant) {
  return categoryMembers(caps, category, variant).map(function(m) { return m.species })
}

// Resolve the sibling layer for an allergen species at a given variant.
function allergenSibling(caps, layer, variant) {
  if (!layer || layer.category !== "allergens") return null
  var members = layersInCategory(caps, "allergens")
  for (var i = 0; i < members.length; i++) {
    if (members[i].species === layer.species && members[i].variant === variant)
      return members[i]
  }
  return null
}

// Friendly label for a WMS style. The layer's first style is its default; the
// alternates are palette variants (Reds, Purples, BuYlRd, …), so surface the
// palette name rather than the raw id.
function styleLabel(name, isDefault) {
  if (isDefault) return "Default"
  var n = String(name || "")
  var palettes = [
    ["BuYlRd", "Blue-Yellow-Red"], ["Reds", "Reds"], ["Purples", "Purples"],
    ["Greens", "Greens"], ["Oranges", "Oranges"], ["Blues", "Blues"], ["all", "All colours"]
  ]
  for (var i = 0; i < palettes.length; i++) {
    if (n.indexOf(palettes[i][0]) !== -1) {
      return palettes[i][1] + (n.indexOf("lowthreshold") !== -1 ? " (low)" : "")
    }
  }
  var s = n.replace(/^sh_/, "").replace(/_/g, " ").trim()
  return s.length ? s : "Style"
}

// Every layer as a searchable option — curated first (category · friendly name),
// then the advanced long tail (technical stem). For the ⚙ all-layers search.
function allLayerOptions(caps) {
  var layers = caps && caps.layers ? caps.layers : []
  var curated = [], advanced = []
  for (var i = 0; i < layers.length; i++) {
    var l = layers[i]
    if (l.tier === "curated")
      curated.push({ value: l.name, label: groupLabel(l.category) + " · " + l.short })
    else
      advanced.push({ value: l.name, label: l.short })
  }
  return curated.concat(advanced)
}

// The layer name a category should open to (its default species, index variant
// for allergens), falling back to the first available member.
function defaultLayerName(caps, category) {
  var members = layersInCategory(caps, category)
  if (members.length === 0) return ""
  var species = DEFAULT_SPECIES[category]
  var preferIndex = category === "allergens"
  var fallback = members[0]
  for (var i = 0; i < members.length; i++) {
    if (members[i].species === species) {
      if (!preferIndex || members[i].variant === "index") return members[i].name
      fallback = members[i]
    }
  }
  return fallback.name
}

// ---------------------------------------------------------------------------
// Web-mercator slippy-map math (EPSG:3857). No QtLocation on the box, so the
// map is hand-rolled: XYZ raster base tiles + a single WMS overlay per viewport.
// "World pixels" = the 256·2^z square the whole world maps to at zoom z.
// ---------------------------------------------------------------------------

var TILE = 256
var MERC_MAX = 20037508.342789244  // half the EPSG:3857 extent, metres

function worldSize(zoom) { return TILE * Math.pow(2, zoom) }

function lonToWorldX(lon, zoom) {
  return (lon + 180) / 360 * worldSize(zoom)
}

function latToWorldY(lat, zoom) {
  var clamped = Math.max(-85.05112878, Math.min(85.05112878, lat))
  var rad = clamped * Math.PI / 180
  var y = (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2
  return y * worldSize(zoom)
}

function worldXToLon(x, zoom) {
  return x / worldSize(zoom) * 360 - 180
}

function worldYToLat(y, zoom) {
  var n = Math.PI - 2 * Math.PI * y / worldSize(zoom)
  return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)))
}

// World pixel (any zoom) → EPSG:3857 metre easting/northing. Y is flipped:
// world-pixel y grows downward (north at top), mercator northing grows up.
function worldXToMerc(x, zoom) {
  return x / worldSize(zoom) * (2 * MERC_MAX) - MERC_MAX
}

function worldYToMerc(y, zoom) {
  return MERC_MAX - y / worldSize(zoom) * (2 * MERC_MAX)
}

// Given a viewport of `width`×`height` pixels centred on (lat,lon) at `zoom`,
// return the list of base tiles covering it, each with its top-left screen
// position. Wraps x around the date line; clamps y to valid rows.
function tilesForViewport(lat, lon, zoom, width, height) {
  var cx = lonToWorldX(lon, zoom)
  var cy = latToWorldY(lat, zoom)
  var originX = cx - width / 2   // world-pixel coord of the viewport's left edge
  var originY = cy - height / 2
  var n = Math.pow(2, zoom)
  var minTX = Math.floor(originX / TILE)
  var maxTX = Math.floor((originX + width) / TILE)
  var minTY = Math.max(0, Math.floor(originY / TILE))
  var maxTY = Math.min(n - 1, Math.floor((originY + height) / TILE))
  var tiles = []
  for (var tx = minTX; tx <= maxTX; tx++) {
    for (var ty = minTY; ty <= maxTY; ty++) {
      var wrappedX = ((tx % n) + n) % n
      tiles.push({
        z: zoom, x: wrappedX, y: ty,
        screenX: tx * TILE - originX,
        screenY: ty * TILE - originY
      })
    }
  }
  return tiles
}

// EPSG:3857 bbox (minx,miny,maxx,maxy metres) for the same viewport — the
// exact rectangle to request as a single WMS GetMap overlay.
function viewportMercBbox(lat, lon, zoom, width, height) {
  var cx = lonToWorldX(lon, zoom)
  var cy = latToWorldY(lat, zoom)
  return {
    minx: worldXToMerc(cx - width / 2, zoom),
    maxx: worldXToMerc(cx + width / 2, zoom),
    // world-y top edge is the max northing, bottom edge is the min northing
    maxy: worldYToMerc(cy - height / 2, zoom),
    miny: worldYToMerc(cy + height / 2, zoom)
  }
}

// Shift the centre by a screen-pixel drag delta (dx,dy = how far the content
// moved) and return the new {lat, lon}.
function panCenter(lat, lon, zoom, dx, dy) {
  var wx = lonToWorldX(lon, zoom) - dx
  var wy = latToWorldY(lat, zoom) - dy
  return { lat: worldYToLat(wy, zoom), lon: worldXToLon(wx, zoom) }
}

var WMS_BASE = "https://eccharts.ecmwf.int/wms/?token=public"

// Build a WMS 1.3.0 GetMap URL for the overlay. For EPSG:3857 the axis order
// is easting,northing, so bbox = minx,miny,maxx,maxy.
function wmsOverlayUrl(layer, style, bbox, width, height, dimTime) {
  var params = [
    "service=WMS", "version=1.3.0", "request=GetMap",
    "layers=" + encodeURIComponent(layer),
    "styles=" + encodeURIComponent(style || ""),
    "crs=EPSG:3857",
    "bbox=" + [bbox.minx, bbox.miny, bbox.maxx, bbox.maxy].join(","),
    "width=" + Math.round(width), "height=" + Math.round(height),
    "format=image/png", "transparent=true"
  ]
  if (dimTime) params.push("dim_time=" + encodeURIComponent(dimTime))
  return WMS_BASE + "&" + params.join("&")
}

// Carto raster basemap, theme-aware. Public tiles; a/b/c/d subdomains.
function baseTileUrl(tile, dark) {
  var style = dark ? "dark_all" : "light_all"
  var sub = "abcd".charAt((tile.x + tile.y) % 4)
  return "https://" + sub + ".basemaps.cartocdn.com/" + style
    + "/" + tile.z + "/" + tile.x + "/" + tile.y + ".png"
}
