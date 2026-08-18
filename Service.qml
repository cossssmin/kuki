import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

Item {
  id: root

  property var shell: null
  property var caps: ({ generatedAt: 0, layerCount: 0, layers: [] })
  property var state: ({ layer: "composition_aod550", style: "sh_all_aod", timeIndex: -1, overlayOpacity: 0.6, enabledSpecies: ({}), lastLayer: ({}), custom: false, home: ({ lat: 49, lon: 15 }), barMetric: "composition_europe_pm2p5_forecast_surface" })
  property bool refreshing: false
  property string lastError: ""
  property var swatches: []
  property bool legendPending: false

  signal panelToggleRequested()
  signal panelOpenRequested()
  signal panelCloseRequested()

  property bool playing: false

  readonly property string home: Quickshell.env("HOME")
  readonly property string baseDir: home + "/.config/omarchy/kuki"
  readonly property string capsPath: baseDir + "/caps.json"
  readonly property string statePath: baseDir + "/state.json"
  readonly property string helperPath: {
    var url = String(Qt.resolvedUrl("cams.py"))
    return decodeURIComponent(url.indexOf("file://") === 0 ? url.substring(7) : url)
  }

  // Currently selected layer's metadata and expanded forecast time steps.
  readonly property var currentLayer: Model.findLayer(root.caps, root.state.layer)
  readonly property var times: currentLayer ? Model.expandTimes(currentLayer.time) : []
  readonly property int timeIndex: {
    if (times.length === 0) return 0
    var idx = Number(root.state.timeIndex)
    if (!isFinite(idx) || idx < 0) return Model.nearestTimeIndex(times, Date.now())
    return Math.max(0, Math.min(times.length - 1, idx))
  }
  readonly property string validTime: times.length ? times[root.timeIndex] : ""

  // Air quality curates the high-res Europe layers inside the CAMS regional
  // domain and the coarser global layers outside it; Allergens (Europe-only
  // pollen) is disabled outside. The region tracks the MAP CENTRE, so panning
  // into North America etc. swaps the overlay to global data. The map opens on
  // the timezone location (cams.py picks a matching first-run layer).
  readonly property string region: {
    var c = root.state.center || root.state.home
    return (c && isFinite(c.lat) && isFinite(c.lon)) ? Model.regionForPoint(c.lat, c.lon) : "europe"
  }
  readonly property var scopedCaps: Model.withRegion(root.caps, root.region)

  // When the region changes (pan) or caps arrive, swap a curated layer that has
  // no data here for its equivalent in the current region. Region-agnostic
  // layers (aerosols/UV) and Other-tab layers are left alone. Pollen has no
  // global twin, so falls back to the Air quality default outside Europe.
  onRegionChanged: reconcileRegionLayer()
  function reconcileRegionLayer() {
    if (!root.caps || !root.caps.layers || !root.caps.layers.length) return
    var l = root.currentLayer
    if (!l || l.tier !== "curated" || (l.region || "any") === "any") return
    if (l.region === root.region) return
    var eq = Model.regionEquivalent(root.caps, l, root.region)
    if (eq) { if (eq !== root.state.layer) setLayer(eq); return }
    setCategory("air-quality")
  }

  // Curated-category navigation, plus an "Other" tab that searches any layer.
  readonly property var categories: Model.availableCategories(root.scopedCaps).concat([{ key: "custom", label: "Other" }])
  readonly property string currentCategory: currentLayer ? currentLayer.category : "air-quality"
  // Custom view: entered explicitly, or whenever the layer is an advanced one
  // (which has no curated chip of its own).
  readonly property bool customMode: root.state.custom === true
    || (currentLayer && currentLayer.category === "advanced")
  readonly property string currentView: customMode ? "custom" : currentCategory
  readonly property string currentVariant: currentLayer ? (currentLayer.variant || "") : ""
  readonly property bool hasVariants: currentCategory === "allergens" && !customMode
  // The Dropdown shows the raw layer id if its value isn't among the options,
  // which happens during the brief load window before caps/state settle. Always
  // guarantee the current layer resolves to a friendly label so no raw id shows.
  readonly property var categoryOptions: {
    if (!root.currentLayer)
      return root.state.layer ? [{ value: root.state.layer, label: "Loading…" }] : []
    var opts = Model.categoryOptions(root.scopedCaps, root.currentCategory, root.currentVariant || "index", root.storedEnabled)
    for (var i = 0; i < opts.length; i++)
      if (opts[i].value === root.state.layer) return opts
    return [{ value: root.state.layer, label: root.currentLayer.short }].concat(opts)
  }

  // Per-category enable checklist (keyed by stable species). Stored value or,
  // when unset/empty, every species (so nothing is hidden by default).
  readonly property var storedEnabled: {
    var m = root.state.enabledSpecies
    return (m && m[root.currentCategory]) ? m[root.currentCategory] : null
  }
  readonly property var speciesOptions: Model.categorySpeciesOptions(root.scopedCaps, currentCategory, currentVariant || "index")
  readonly property var enabledSpecies: (storedEnabled && storedEnabled.length)
    ? storedEnabled
    : Model.allSpecies(root.scopedCaps, currentCategory, currentVariant || "index")

  readonly property var legendEnds: Model.legendEnds(currentCategory)
  readonly property var allLayerOptions: Model.allLayerOptions(root.caps)
  readonly property var styleOptions: {
    if (!currentLayer || !currentLayer.styles) return []
    var out = []
    for (var i = 0; i < currentLayer.styles.length; i++)
      out.push({ value: currentLayer.styles[i], label: Model.styleLabel(currentLayer.styles[i], i === 0) })
    return out
  }

  function runHelper(args) {
    if (helperProcess.running) return false
    root.refreshing = true
    root.lastError = ""
    helperProcess.command = ["python3", root.helperPath].concat(args)
    helperProcess.running = true
    return true
  }

  function refresh() {
    return runHelper(["capabilities", "--force"])
  }

  // Discrete legend swatches for the active layer/style, extracted from the WMS
  // legend by the helper. Recomputed whenever the layer or style changes.
  readonly property string legendKey: (root.state.layer || "") + "|" + (root.state.style || "")
  onLegendKeyChanged: refreshLegend()

  function refreshLegend() {
    if (!root.state.layer) { root.swatches = []; return }
    if (legendProcess.running) { root.legendPending = true; return }
    legendProcess.command = ["python3", root.helperPath, "legend",
      "--layer", root.state.layer, "--style", root.state.style || ""]
    legendProcess.running = true
  }

  Process {
    id: legendProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = {}
        try { parsed = JSON.parse(text || "{}") } catch (e) { parsed = {} }
        if (parsed.layer === root.state.layer)
          root.swatches = Array.isArray(parsed.colors) ? parsed.colors : []
      }
    }
    onExited: {
      if (root.legendPending) { root.legendPending = false; root.refreshLegend() }
    }
  }

  function setLayer(name) {
    if (!name) return
    var layer = Model.findLayer(root.caps, name)
    var next = Object.assign({}, root.state)
    next.layer = name
    // Each layer carries its own styles; the previous layer's style is invalid
    // for a different variable, so adopt this layer's default (first) style.
    next.style = (layer && layer.styles && layer.styles.length) ? layer.styles[0] : ""
    next.timeIndex = -1
    // Remember this as the category's last-viewed layer, so returning to the
    // category's chip restores it rather than resetting to the default.
    if (layer) {
      var last = Object.assign({}, next.lastLayer || {})
      last[layer.category] = name
      next.lastLayer = last
    }
    root.state = next
    persist()
  }

  function setCustom(on) {
    var next = Object.assign({}, root.state)
    next.custom = on
    root.state = next
    persist()
  }

  function setCategory(category) {
    if (category === "custom") { setCustom(true); return }
    // Ignore categories with no layers in the current region (a disabled tab,
    // e.g. Allergens outside Europe), so nothing lands on an empty picker.
    if (Model.layersInCategory(root.scopedCaps, category).length === 0) return
    var wasCustom = root.customMode
    setCustom(false)
    if (!wasCustom && category === root.currentCategory) return

    var enabled = (root.state.enabledSpecies && root.state.enabledSpecies[category]
      && root.state.enabledSpecies[category].length) ? root.state.enabledSpecies[category] : null
    // Restore the category's last-viewed layer if it still exists and isn't
    // hidden by the Layers checklist; otherwise fall back to the first shown
    // option (or the category default).
    var remembered = root.state.lastLayer && root.state.lastLayer[category]
    if (remembered) {
      var rl = Model.findLayer(root.caps, remembered)
      if (rl && (!enabled || enabled.indexOf(rl.species) !== -1)) { setLayer(remembered); return }
    }
    // No valid remembered layer: the category default, unless it's hidden by the
    // checklist, in which case the first shown option.
    var target = Model.defaultLayerName(root.scopedCaps, category)
    var dl = Model.findLayer(root.caps, target)
    if (enabled && dl && enabled.indexOf(dl.species) === -1) {
      var opts = Model.categoryOptions(root.scopedCaps, category, "index", enabled)
      if (opts.length) target = opts[0].value
    }
    setLayer(target)
  }

  function setEnabledSpecies(arr) {
    var next = Object.assign({}, root.state)
    var map = Object.assign({}, next.enabledSpecies || {})
    map[root.currentCategory] = arr
    next.enabledSpecies = map
    root.state = next
    persist()
    // If the current layer's species just got unchecked, jump to the first
    // still-enabled option so the view never rests on a hidden layer.
    var effective = (arr && arr.length)
      ? arr : Model.allSpecies(root.scopedCaps, root.currentCategory, root.currentVariant || "index")
    var species = root.currentLayer ? root.currentLayer.species : null
    if (species && effective.indexOf(species) === -1) {
      var opts = Model.categoryOptions(root.scopedCaps, root.currentCategory, root.currentVariant || "index", effective)
      if (opts.length) setLayer(opts[0].value)
    }
  }

  function stepLayer(delta) {
    var opts = root.categoryOptions
    if (!opts || opts.length === 0) return
    var idx = 0
    for (var i = 0; i < opts.length; i++)
      if (opts[i].value === root.state.layer) { idx = i; break }
    var next = ((idx + delta) % opts.length + opts.length) % opts.length
    setLayer(opts[next].value)
  }

  function stepCategory(delta) {
    var cats = root.categories
    if (!cats || cats.length === 0) return
    var idx = 0
    for (var i = 0; i < cats.length; i++)
      if (cats[i].key === root.currentView) { idx = i; break }
    // Skip disabled tabs (e.g. Allergens outside Europe) so stepping never
    // stalls on an unreachable category.
    var step = delta >= 0 ? 1 : -1
    for (var n = 0; n < cats.length; n++) {
      idx = ((idx + step) % cats.length + cats.length) % cats.length
      if (!cats[idx].disabled) { setCategory(cats[idx].key); return }
    }
  }

  function setVariant(variant) {
    var sibling = Model.allergenSibling(root.scopedCaps, root.currentLayer, variant)
    if (sibling) setLayer(sibling.name)
  }

  function setTimeIndex(idx) {
    if (root.times.length === 0) return
    var clamped = Math.max(0, Math.min(root.times.length - 1, Math.round(idx)))
    var next = Object.assign({}, root.state)
    next.timeIndex = clamped
    root.state = next
    persist()
  }

  // Reset the scrubber to "auto" (-1), so timeIndex recomputes to the step
  // nearest the current clock. Called each time the panel opens, so reopening
  // hours later lands on the current forecast instead of the last-viewed step.
  function snapTimeToNow() {
    if (Number(root.state.timeIndex) < 0) return
    var next = Object.assign({}, root.state)
    next.timeIndex = -1
    root.state = next
    persist()
  }

  function stepTime(delta) {
    setTimeIndex(root.timeIndex + delta)
  }

  function togglePlay() { root.playing = !root.playing }
  function setPlaying(value) { root.playing = value }

  // Advance one step, wrapping at the end. The Panel drives play cadence off the
  // map's actual frame-ready events (network-paced), not a fixed timer.
  function advanceLoop() {
    if (root.times.length < 2) { root.playing = false; return }
    var nextIndex = root.timeIndex + 1
    root.setTimeIndex(nextIndex >= root.times.length ? 0 : nextIndex)
  }

  onTimesChanged: if (root.times.length === 0) root.playing = false

  function setView(lat, lon, zoom) {
    var next = Object.assign({}, root.state)
    next.center = { lat: lat, lon: lon }
    next.zoom = Math.round(zoom)
    root.state = next
    persist()
  }

  function setOverlayOpacity(value) {
    var next = Object.assign({}, root.state)
    next.overlayOpacity = Math.max(0, Math.min(1, value))
    root.state = next
    persist()
  }

  function setStyle(name) {
    var next = Object.assign({}, root.state)
    next.style = name
    root.state = next
    persist()
  }

  function persist() {
    stateFile.setText(JSON.stringify(root.state))
  }

  // --- Bar readout: periodic value probe at the home location ---------------
  property real barValue: NaN
  property string barUnit: ""
  readonly property var barLayer: Model.findLayer(root.caps, root.state.barMetric || "")
  readonly property var barTimes: barLayer ? Model.expandTimes(barLayer.time) : []
  readonly property string barTime: barTimes.length ? barTimes[Model.nearestTimeIndex(barTimes, Date.now())] : ""
  readonly property var barLevel: Model.levelFor(Model.metricKind(barLayer), barValue)
  readonly property string barSummary: {
    if (!barLayer) return "Kūki"
    if (!isFinite(barValue)) return barLayer.short + " · …"
    var v = barValue < 10 ? barValue.toFixed(1) : Math.round(barValue)
    return barLayer.short + " " + v + (barUnit ? " " + barUnit : "")
      + (barLevel ? " · " + barLevel.name : "")
  }

  function refreshBar() {
    if (barProcess.running || !barLayer || !root.state.home || !root.barTime) return
    barProcess.command = ["python3", root.helperPath, "probe",
      "--layer", root.state.barMetric, "--style", "",
      "--lat", String(root.state.home.lat), "--lon", String(root.state.home.lon),
      "--time", root.barTime]
    barProcess.running = true
  }

  Process {
    id: barProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = {}
        try { parsed = JSON.parse(text || "{}") } catch (e) { parsed = {} }
        if (parsed.value !== null && isFinite(parsed.value)) {
          root.barValue = Number(parsed.value)
          root.barUnit = parsed.unit || ""
        }
      }
    }
  }

  Timer {
    id: barTimer
    interval: 30 * 60 * 1000  // 30 min
    repeat: true
    running: true
    onTriggered: root.refreshBar()
  }

  onBarLayerChanged: if (barLayer) Qt.callLater(refreshBar)

  Component.onCompleted: { Qt.callLater(refreshLegend); Qt.callLater(refreshBar) }

  Process {
    id: initProcess
    command: ["python3", root.helperPath, "init"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        capsFile.reload()
        stateFile.reload()
      }
    }
    Component.onCompleted: running = true
    onExited: function(exitCode) {
      if (exitCode !== 0) root.lastError = "Could not initialize Kūki"
    }
  }

  Process {
    id: helperProcess
    stdout: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      root.refreshing = false
      if (exitCode !== 0) root.lastError = "Could not refresh capabilities"
      capsFile.reload()
    }
  }

  // Silent periodic refresh: the helper only re-fetches when the cache is stale
  // (>6h), so an hourly tick keeps the forecast current on a long-running shell
  // without touching the visible refreshing state.
  Timer {
    id: autoRefreshTimer
    interval: 60 * 60 * 1000
    repeat: true
    running: true
    onTriggered: {
      if (autoRefreshProcess.running) return
      autoRefreshProcess.command = ["python3", root.helperPath, "capabilities"]
      autoRefreshProcess.running = true
    }
  }

  Process {
    id: autoRefreshProcess
    stdout: StdioCollector { waitForEnd: true }
    onExited: capsFile.reload()
  }

  FileView {
    id: capsFile
    path: root.capsPath
    watchChanges: true
    printErrors: false
    onLoaded: { root.caps = Model.parseCaps(text()); Qt.callLater(root.reconcileRegionLayer) }
    onFileChanged: reload()
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onLoaded: { root.state = Object.assign({}, root.state, Model.parseState(text())); Qt.callLater(root.reconcileRegionLayer) }
    onFileChanged: reload()
  }

  IpcHandler {
    target: "kuki"

    function status(): string {
      return JSON.stringify({
        layer: root.state.layer,
        layerCount: root.caps.layerCount,
        generatedAt: root.caps.generatedAt,
        steps: root.times.length,
        timeIndex: root.timeIndex,
        validTime: root.validTime,
        refreshing: root.refreshing,
        error: root.lastError
      })
    }

    function refresh(): bool { return root.refresh() }
    function next(): void { root.stepTime(1) }
    function prev(): void { root.stepTime(-1) }
    function play(): void { root.setPlaying(true) }
    function pause(): void { root.setPlaying(false) }
    function togglePlay(): void { root.togglePlay() }
    function toggle(): void { root.panelToggleRequested() }
    function open(): void { root.panelOpenRequested() }
    function close(): void { root.panelCloseRequested() }
  }
}
