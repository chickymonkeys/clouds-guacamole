import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "chickymonkeys.guacamole"
  ipcTarget: "chickymonkeys.guacamole"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // The plugin's root folder, wherever it was installed (copy, symlink or git clone)
  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl("..")).replace(/^file:\/\//, "")).replace(/\/$/, "")

  // The theme has no green or amber, so these muted tones sit next to Color.urgent
  readonly property color statusColor: service.statusState === "error" ? root.urgent
                                       : (service.statusState === "online" ? "#6fa96f" : "#c9a24a")

  readonly property string barTooltipText: {
    if (!service.connected) return "Clouds: " + (service.startError || "starting…")
    if (service.actionFailed && service.lastError !== "") return "Clouds: " + service.lastError
    if (!service.hasDrives) return "Clouds: click to connect a drive"
    var t = "Clouds: " + service.statusText
    if (service.transfers.count > 0 && service.transfers.speed > 0) t += " · " + Model.formatSpeed(service.transfers.speed)
    if (service.summary.attention > 0) t += " · " + service.summary.attention + " need attention"
    return t
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    service.touch()
    content.resetScroll()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: service
    settings: root.settings
    pluginDir: root.pluginDir
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function add(): void {
      root.open()
      content.showView("add")
    }
    function settings(): void {
      root.open()
      content.showView("settings")
    }
    function syncAll(): string { service.syncNow(""); return "ok" }
    function streamAll(): string { service.streamAll(true); return "ok" }
    function status(): string {
      return JSON.stringify({ connected: service.connected, summary: service.summary, text: service.statusText })
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: root.barTooltipText

    iconComponent: Component {
      Item {
        anchors.fill: parent

        CloudIcon {
          anchors.centerIn: parent
          iconSize: Style.bar.iconFont
          color: root.statusColor
          fontFamily: root.fontFamily
          active: service.statusState === "online"
          busy: service.busy
          attention: service.attention
        }
      }
    }

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) service.syncNow("")
      else if (buttonCode === Qt.MiddleButton) service.streamAll(!service.allStreaming)
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(content.preferredHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: {
        if (content.currentView === "log") content.showView("settings")
        else if (content.currentView !== "drives") content.showView("drives")
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (content.currentView !== "drives") return
        var k = t.toLowerCase()
        if (k === "a") content.showView("add")
        else if (k === "s") content.showView("settings")
        else if (k === "r") service.syncNow("")
        else if (k === "m") service.streamAll(!service.allStreaming)
        else if (k === "o") service.openPath(service.cloudRoot)
      }

      PanelContent {
        id: content
        anchors.fill: parent
        service: service
        foreground: root.foreground
        urgent: root.urgent
        fontFamily: root.fontFamily
        statusColor: root.statusColor
      }
    }
  }
}
