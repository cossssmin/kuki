import QtQuick
import QtQuick.Effects
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "kuki"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var kukiService: null
  readonly property var barIdentity: hostWidget || root
  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool dark:
    (0.299 * Color.background.r + 0.587 * Color.background.g + 0.114 * Color.background.b) < 0.5

  readonly property var svc: kukiService
  readonly property int layerCount: svc ? svc.caps.layerCount : 0
  readonly property var currentLayer: svc ? svc.currentLayer : null
  readonly property int steps: svc ? svc.times.length : 0
  readonly property int timeIndex: svc ? svc.timeIndex : 0
  readonly property string validTime: svc ? svc.validTime : ""
  readonly property bool refreshing: svc ? svc.refreshing : false
  readonly property var center: (svc && svc.state && svc.state.center) ? svc.state.center : ({ lat: 40, lon: 10 })
  property bool settingsOpen: false

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? root.close() : root.open() }

  // Play cadence is paced by the map's actual frame loads, so a slow network
  // never outruns the overlay. Each advance arms a safety timeout; a ready frame
  // arms a short dwell before the next step.
  function playAdvance() {
    if (!root.svc || !root.svc.playing) return
    root.svc.advanceLoop()
    playSafety.restart()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  Connections {
    target: root.svc
    function onPlayingChanged() { if (root.svc && root.svc.playing) root.playAdvance() }
  }

  Connections {
    target: map
    function onOverlayFrameReady() {
      if (root.svc && root.svc.playing) playDwell.restart()
    }
  }

  Timer { id: playDwell; interval: 550; onTriggered: root.playAdvance() }
  Timer { id: playSafety; interval: 4000; onTriggered: root.playAdvance() }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyScope
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    // Own key handling (PanelKeyCatcher strips modifiers, so it can't see
    // Ctrl+arrows): Space play/pause, arrows step, Ctrl+arrows switch category,
    // Esc close, Tab move between bar panels.
    FocusScope {
      id: keyScope
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        var ctrl = (event.modifiers & Qt.ControlModifier) !== 0
        if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        else if (event.key === Qt.Key_Tab) { root.switchPanel(1); event.accepted = true }
        else if (event.key === Qt.Key_Backtab) { root.switchPanel(-1); event.accepted = true }
        else if (event.key === Qt.Key_Space) {
          if (root.svc) root.svc.togglePlay(); event.accepted = true
        } else if (event.key === Qt.Key_Left) {
          if (root.svc) ctrl ? root.svc.stepCategory(-1) : root.svc.stepTime(-1)
          event.accepted = true
        } else if (event.key === Qt.Key_Right) {
          if (root.svc) ctrl ? root.svc.stepCategory(1) : root.svc.stepTime(1)
          event.accepted = true
        } else if (event.key === Qt.Key_Up) {
          if (root.svc) root.svc.stepLayer(-1); event.accepted = true
        } else if (event.key === Qt.Key_Down) {
          if (root.svc) root.svc.stepLayer(1); event.accepted = true
        }
      }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(10)

        // Header: mark + current layer + refresh.
        Item {
          width: parent.width
          height: Math.max(glyph.height, headerText.height, refreshButton.height)

          Text {
            id: glyph
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "󰖝"
            color: Color.accent
            font.family: root.contentFontFamily
            font.pixelSize: Style.space(30)
          }

          Button {
            id: refreshButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: "󰑐"
            iconSpinning: root.refreshing
            tooltipText: "Refresh capabilities"
            enabled: !root.refreshing
            focusable: true
            bordered: true
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: if (root.svc) root.svc.refresh()
          }

          Button {
            id: settingsButton
            anchors.right: refreshButton.left
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            text: "󰢻"
            tooltipText: "Settings"
            selected: root.settingsOpen
            focusable: true
            bordered: true
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: root.settingsOpen = !root.settingsOpen
          }

          Column {
            id: headerText
            anchors.left: glyph.right
            anchors.leftMargin: Style.space(12)
            anchors.right: settingsButton.left
            anchors.rightMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            Text {
              text: "Kūki"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              width: parent.width
              elide: Text.ElideRight
              text: root.currentLayer ? Model.cleanTitle(root.currentLayer.title)
                : (root.layerCount > 0 ? (root.layerCount + " CAMS layers") : "Loading…")
              color: Util.alpha(root.contentForeground, 0.64)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        // Category chips.
        Flow {
          width: parent.width
          spacing: Style.space(6)

          Repeater {
            model: root.svc ? root.svc.categories : []

            Button {
              required property var modelData
              text: modelData.label
              selected: root.svc && root.svc.currentView === modelData.key
              focusable: true
              bordered: true
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.svc) root.svc.setCategory(modelData.key)
            }
          }
        }

        // Layer picker for the active category (+ allergen concentration/index toggle).
        Row {
          width: parent.width
          spacing: Style.space(8)
          visible: !root.svc || !root.svc.customMode

          Dropdown {
            id: layerPicker
            width: root.svc && root.svc.hasVariants
              ? parent.width - variantToggle.width - Style.space(8)
              : parent.width
            showLabel: false
            fontFamily: root.contentFontFamily
            options: root.svc ? root.svc.categoryOptions : []
            onChanged: function(value) { if (root.svc) root.svc.setLayer(value) }
            // The Dropdown assigns its own `value` on select, breaking a plain
            // binding; a Binding element re-asserts state whenever it changes
            // (e.g. switching category via the chips).
            Binding {
              target: layerPicker; property: "value"
              value: root.svc ? root.svc.state.layer : ""
            }
          }

          Button {
            id: variantToggle
            anchors.verticalCenter: parent.verticalCenter
            visible: root.svc && root.svc.hasVariants
            text: root.svc && root.svc.currentVariant === "index" ? "Index" : "Levels"
            tooltipText: "Toggle banded index vs concentration"
            focusable: true
            bordered: true
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: if (root.svc)
              root.svc.setVariant(root.svc.currentVariant === "index" ? "concentration" : "index")
          }
        }

        // Custom tab: search any of the 95 layers instead of a category picker.
        SearchableDropdown {
          id: customSearch
          width: parent.width
          visible: root.svc && root.svc.customMode
          showLabel: false
          placeholderText: "Search all layers…"
          triggerLabel: "Search all layers…"
          fontFamily: root.contentFontFamily
          options: root.svc ? root.svc.allLayerOptions : []
          onChanged: function(value) { if (root.svc) root.svc.setLayer(value) }
          Binding {
            target: customSearch; property: "value"
            value: root.svc ? root.svc.state.layer : ""
          }
        }

        // Settings — opacity and style, revealed by the ⚙ button so the panel
        // stays compact by default.
        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: root.settingsOpen

          Item {
            width: parent.width
            height: Math.max(opacityLabel.height, opacitySlider.height)

            Text {
              id: opacityLabel
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(64)
              text: "Opacity"
              color: Util.alpha(root.contentForeground, 0.8)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }

            PanelSlider {
              id: opacitySlider
              anchors.left: opacityLabel.right
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              bar: root.bar
              minimum: 0.15
              maximum: 1.0
              step: 0.05
              value: root.svc ? root.svc.state.overlayOpacity : 0.6
              onMoved: function(v) { if (root.svc) root.svc.setOverlayOpacity(v) }
              onReleased: function(v) { if (root.svc) root.svc.setOverlayOpacity(v) }
            }
          }

          Item {
            width: parent.width
            height: styleDropdown.height
            visible: root.svc && root.svc.styleOptions.length > 1

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(64)
              text: "Style"
              color: Util.alpha(root.contentForeground, 0.8)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }

            Dropdown {
              id: styleDropdown
              anchors.left: parent.left
              anchors.leftMargin: Style.space(64)
              anchors.right: parent.right
              showLabel: false
              fontFamily: root.contentFontFamily
              options: root.svc ? root.svc.styleOptions : []
              onChanged: function(value) { if (root.svc) root.svc.setStyle(value) }
              Binding {
                target: styleDropdown; property: "value"
                value: root.svc ? root.svc.state.style : ""
              }
            }
          }

          // Per-category layer checklist: tick which of this category's layers
          // appear in its picker. Hidden on the Custom tab (no category there).
          Item {
            width: parent.width
            height: layersSelect.height
            visible: root.svc && !root.svc.customMode

            Text {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(64)
              text: "Layers"
              color: Util.alpha(root.contentForeground, 0.8)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }

            MultiSelect {
              id: layersSelect
              anchors.left: parent.left
              anchors.leftMargin: Style.space(64)
              anchors.right: parent.right
              showLabel: false
              triggerLabel: root.svc ? (root.svc.enabledSpecies.length + " shown") : "Layers"
              fontFamily: root.contentFontFamily
              options: root.svc ? root.svc.speciesOptions : []
              onChanged: function(values) { if (root.svc) root.svc.setEnabledSpecies(values) }
              Binding {
                target: layersSelect; property: "values"
                value: root.svc ? root.svc.enabledSpecies : []
              }
            }
          }
        }

        // The map. A MultiEffect rounded-rect mask clips the content to the
        // corner radius (plain clip only clips to the square bounds).
        Item {
          width: parent.width
          height: Style.space(360)

          Rectangle {
            id: mapMask
            anchors.fill: parent
            radius: Style.cornerRadius
            visible: false
            layer.enabled: true
          }

          Item {
            anchors.fill: parent
            layer.enabled: true
            layer.smooth: true
            layer.effect: MultiEffect {
              maskEnabled: true
              maskSource: mapMask
              maskThresholdMin: 0.5
              maskSpreadAtMin: 1.0
            }

            MapSurface {
              id: map
              anchors.fill: parent
              dark: root.dark
              lat: root.center.lat
              lon: root.center.lon
              zoom: root.svc && root.svc.state.zoom ? root.svc.state.zoom : 3
              overlayLayer: root.svc ? root.svc.state.layer : ""
              overlayStyle: root.svc ? root.svc.state.style : ""
              overlayTime: root.validTime
              overlayOpacity: root.svc ? root.svc.state.overlayOpacity : 0.75
              onViewChanged: function(lat, lon, zoom) {
                if (root.svc) root.svc.setView(lat, lon, zoom)
              }
            }
          }

          // Border drawn on top of the masked content.
          Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: "transparent"
            border.width: 1
            border.color: Util.alpha(root.contentForeground, 0.18)
          }
        }

        // Legend — minimal discrete swatches below the map, low→high. Square
        // corners follow the Omarchy rounding setting.
        Row {
          width: parent.width
          spacing: Style.space(5)
          visible: root.svc && root.svc.swatches.length > 0

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.svc ? root.svc.legendEnds.low : "Low"
            color: Util.alpha(root.contentForeground, 0.64)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
          }

          Row {
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(3)

            Repeater {
              model: root.svc ? root.svc.swatches : []

              Rectangle {
                required property var modelData
                width: Style.space(15)
                height: Style.space(15)
                radius: Math.min(Style.cornerRadius, width / 2)
                color: modelData
                border.width: 1
                border.color: Util.alpha(root.contentForeground, 0.18)
              }
            }
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.svc ? root.svc.legendEnds.high : "High"
            color: Util.alpha(root.contentForeground, 0.64)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // Time scrubber — date/step on the left, transport controls grouped right.
        Item {
          width: parent.width
          height: Math.max(transport.height, scrubberLabel.height)

          Column {
            id: scrubberLabel
            anchors.left: parent.left
            anchors.right: transport.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(1)

            Text {
              width: parent.width
              elide: Text.ElideRight
              text: root.validTime ? Model.formatTime(root.validTime) : "—"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
              font.bold: true
            }

            Text {
              width: parent.width
              text: root.steps > 0 ? ("step " + (root.timeIndex + 1) + " / " + root.steps) : "no steps"
              color: Util.alpha(root.contentForeground, 0.64)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            id: transport
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            Button {
              text: "󰅁"
              tooltipText: "Previous"
              enabled: root.steps > 0 && root.timeIndex > 0
              focusable: true
              bordered: true
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.svc) root.svc.stepTime(-1)
            }

            Button {
              text: root.svc && root.svc.playing ? "󰏤" : "󰐊"
              tooltipText: root.svc && root.svc.playing ? "Pause" : "Play forecast"
              enabled: root.steps > 1
              focusable: true
              bordered: true
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.svc) root.svc.togglePlay()
            }

            Button {
              text: "󰅂"
              tooltipText: "Next"
              enabled: root.steps > 0 && root.timeIndex < root.steps - 1
              focusable: true
              bordered: true
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: if (root.svc) root.svc.stepTime(1)
            }
          }
        }

        Text {
          visible: root.svc && root.svc.lastError !== ""
          width: parent.width
          text: root.svc ? root.svc.lastError : ""
          color: Color.urgent
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
