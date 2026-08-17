import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "kuki"

  readonly property var kukiService: bar && bar.shell
    ? bar.shell.serviceFor("kuki") : null
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.kukiService = root.kukiService
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onKukiServiceChanged: injectPanel()

  Connections {
    target: root.kukiService
    function onPanelToggleRequested() { root.toggle() }
    function onPanelOpenRequested() { root.open() }
    function onPanelCloseRequested() { root.close() }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰖝"
    // The built-in label centres the glyph's advance box, which leaves the
    // wind glyph looking off. Keep the text for sizing but hide it, and draw an
    // OpticalGlyph on top that centres the actual ink.
    foreground: "transparent"
    tooltipText: root.kukiService ? root.kukiService.barSummary : "Kūki"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
    }
  }

  OpticalGlyph {
    anchors.fill: button
    text: "󰖝"
    fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
    fontSize: Style.font.body
    color: root.bar ? root.bar.barForeground : Color.foreground
  }
}
