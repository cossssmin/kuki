import QtQuick
import qs.Commons
import "Model.js" as Model

// Hand-rolled slippy map: XYZ raster base tiles under a single translucent WMS
// overlay for the exact viewport, panned/zoomed by mouse. No QtLocation.
Item {
  id: root
  clip: true

  // View state (owned by the caller; changes are echoed back through the signals).
  property real lat: 40.0
  property real lon: 10.0
  property int zoom: 3
  property int minZoom: 2
  property int maxZoom: 9
  property bool dark: false

  // Overlay (CAMS layer) inputs.
  property string overlayLayer: ""
  property string overlayStyle: ""
  property string overlayTime: ""
  property real overlayOpacity: 0.6

  signal viewChanged(real lat, real lon, int zoom)
  signal overlayFrameReady()

  readonly property var tiles: Model.tilesForViewport(lat, lon, zoom, width, height)

  // The overlay is double-buffered: a new frame loads into the idle buffer while
  // the current one stays on screen, so stepping/playing never flashes blank.
  // `suppressOverlay` hides it outright during a pan/zoom (the old frame no
  // longer matches the viewport) until the fresh frame is ready.
  property int liveBuffer: -1     // which buffer is shown (-1 = none yet)
  property int pendingBuffer: -1  // which buffer is loading the next frame
  property bool suppressOverlay: false
  readonly property bool overlayLoading: pendingBuffer !== -1

  function scheduleOverlay() { overlayDebounce.restart() }

  function clearOverlay() { bufA.source = ""; bufB.source = ""; liveBuffer = -1; pendingBuffer = -1 }

  function rebuildOverlay() {
    if (!overlayLayer) { clearOverlay(); return }
    // Not laid out yet (panel still opening): retry rather than clear, so the
    // overlay heals itself once the viewport has a real size.
    if (!visible || width < 1 || height < 1) { overlayDebounce.restart(); return }
    var bbox = Model.viewportMercBbox(lat, lon, zoom, width, height)
    applyOverlay(Model.wmsOverlayUrl(overlayLayer, overlayStyle, bbox, width, height, overlayTime))
  }

  function applyOverlay(src) {
    if (!src) { clearOverlay(); return }
    var target = liveBuffer === 0 ? 1 : 0
    var img = target === 0 ? bufA : bufB
    // Mark pending BEFORE touching source: a cached image can flip to Ready
    // synchronously on assignment, firing statusChanged while pendingBuffer is
    // still stale — bufferReady would then bail and leave it stuck loading.
    pendingBuffer = target
    if (String(img.source) !== src) img.source = src
    // A cached/already-loaded image won't emit statusChanged again, so settle now.
    if (img.status === Image.Ready) bufferReady(target)
    else if (img.status === Image.Error) bufferFailed(target)
  }

  function bufferReady(index) {
    if (pendingBuffer !== index) return
    liveBuffer = index
    pendingBuffer = -1
    suppressOverlay = false
    overlayFrameReady()
  }

  function bufferFailed(index) {
    if (pendingBuffer === index) { pendingBuffer = -1; suppressOverlay = false }
  }

  onOverlayLayerChanged: scheduleOverlay()
  onOverlayStyleChanged: scheduleOverlay()
  onOverlayTimeChanged: scheduleOverlay()
  onWidthChanged: scheduleOverlay()
  onHeightChanged: scheduleOverlay()
  onLatChanged: scheduleOverlay()
  onLonChanged: scheduleOverlay()
  onZoomChanged: scheduleOverlay()
  onVisibleChanged: if (visible) scheduleOverlay()
  Component.onCompleted: scheduleOverlay()

  Rectangle {
    anchors.fill: parent
    color: root.dark ? "#1a1a1a" : "#e8e8e8"
  }

  // Base tiles.
  Repeater {
    model: root.tiles
    delegate: Image {
      required property var modelData
      x: modelData.screenX
      y: modelData.screenY
      width: 256
      height: 256
      asynchronous: true
      cache: true
      fillMode: Image.Stretch
      source: Model.baseTileUrl(modelData, root.dark)
    }
  }

  // CAMS overlay — double-buffered GetMap frames.
  Image {
    id: bufA
    anchors.fill: parent
    asynchronous: true
    cache: true
    fillMode: Image.Stretch
    opacity: (root.liveBuffer === 0 && !root.suppressOverlay) ? root.overlayOpacity : 0
    Behavior on opacity { NumberAnimation { duration: 150 } }
    onStatusChanged: {
      if (status === Image.Ready) root.bufferReady(0)
      else if (status === Image.Error) root.bufferFailed(0)
    }
  }

  Image {
    id: bufB
    anchors.fill: parent
    asynchronous: true
    cache: true
    fillMode: Image.Stretch
    opacity: (root.liveBuffer === 1 && !root.suppressOverlay) ? root.overlayOpacity : 0
    Behavior on opacity { NumberAnimation { duration: 150 } }
    onStatusChanged: {
      if (status === Image.Ready) root.bufferReady(1)
      else if (status === Image.Error) root.bufferFailed(1)
    }
  }

  // Loading pill — shown while a frame is being fetched.
  Rectangle {
    visible: root.overlayLoading
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.top: parent.top
    anchors.topMargin: Style.space(8)
    radius: Style.cornerRadius
    color: root.dark ? "#cc1c1c1c" : "#e6ffffff"
    border.width: 1
    border.color: root.dark ? "#33ffffff" : "#22000000"
    implicitWidth: loadingRow.width + Style.space(16)
    implicitHeight: loadingRow.height + Style.space(8)

    Row {
      id: loadingRow
      anchors.centerIn: parent
      spacing: Style.space(6)

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: "󰑐"
        color: root.dark ? "#eeeeee" : "#333333"
        font.pixelSize: Style.font.caption
        RotationAnimator on rotation {
          from: 0; to: 360; duration: 900
          loops: Animation.Infinite
          running: root.overlayLoading
        }
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: "Updating…"
        color: root.dark ? "#eeeeee" : "#333333"
        font.pixelSize: Style.font.caption
      }
    }
  }

  Timer {
    id: overlayDebounce
    interval: 250
    repeat: false
    onTriggered: root.rebuildOverlay()
  }

  MouseArea {
    id: dragArea
    anchors.fill: parent
    hoverEnabled: false
    cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
    property real lastX: 0
    property real lastY: 0

    onPressed: function(mouse) { lastX = mouse.x; lastY = mouse.y }

    onPositionChanged: function(mouse) {
      if (!pressed) return
      // Panning moves the basemap, not the overlay: hide it immediately (the old
      // frame no longer aligns) until the fresh viewport frame is ready.
      root.suppressOverlay = true
      var moved = Model.panCenter(root.lat, root.lon, root.zoom, mouse.x - lastX, mouse.y - lastY)
      root.lat = moved.lat
      root.lon = moved.lon
      lastX = mouse.x
      lastY = mouse.y
      overlayDebounce.restart()
    }

    onReleased: root.viewChanged(root.lat, root.lon, root.zoom)

    onWheel: function(wheel) {
      var step = wheel.angleDelta.y > 0 ? 1 : -1
      var next = Math.max(root.minZoom, Math.min(root.maxZoom, root.zoom + step))
      if (next === root.zoom) return
      root.suppressOverlay = true
      root.zoom = next
      overlayDebounce.restart()
      root.viewChanged(root.lat, root.lon, root.zoom)
    }
  }

  // Attribution — Carto/OSM require it; keep it small and out of the way.
  Text {
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    anchors.margins: Style.space(3)
    text: "© OpenStreetMap, CARTO · CAMS"
    color: root.dark ? "#cccccc" : "#333333"
    opacity: 0.7
    font.pixelSize: Style.font.caption
  }
}
