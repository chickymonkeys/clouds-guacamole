import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Everything inside the popout: the drives overview and the add / settings / folder picker /
// client id views. Kept apart from Panel.qml so it can also be rendered on its own.
Item {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property color urgent: Color.urgent
  property string fontFamily: Style.font.family
  property color statusColor: foreground

  // "drives" | "add" | "settings" | "browse" | "client" | "log"
  property string currentView: "drives"

  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property var summary: service.summary
  readonly property real footerGap: Style.space(10)
  readonly property real preferredHeight: currentView === "drives"
                                          ? drivesCol.implicitHeight + footerGap + footer.implicitHeight
                                          : viewCol.implicitHeight
  // The one thing worth knowing right now; the bar tooltip keeps the full summary
  readonly property string headline: {
    var s = service
    if (!s.connected || s.engine.state !== "running" || !s.hasDrives) return s.statusText
    if (s.summary.attention > 0) return s.summary.attention + (s.summary.attention === 1 ? " folder needs you" : " folders need you")
    if (!s.online) return "Offline"
    if (s.summary.syncing > 0 || s.transfers.count > 0) {
      var t = s.summary.syncing > 0 ? "Syncing" : "Uploading"
      return s.transfers.speed > 0 ? t + " · " + Model.formatSpeed(s.transfers.speed) : t + "…"
    }
    if (s.summary.uploads > 0) return "Uploading " + s.summary.uploads + (s.summary.uploads === 1 ? " file" : " files")
    if (s.summary.mounted < s.summary.streaming) return "Connecting…"
    if (s.summary.streaming === 0 && s.summary.folders === 0) return "Nothing streaming"
    return "Up to date"
  }
  // Unmount anyway, when the daemon refused because of uploads or open files
  readonly property bool canForce: service.lastErrorCode === "uploads_pending" || service.lastErrorCode === "busy"
  // Drives whose local folders are listed, by name; kept here so the choice outlives the cards,
  // which are rebuilt when a drive is added or removed
  property var openFolderLists: ({})

  function setFoldersOpen(remote, open) {
    var lists = Object.assign({}, openFolderLists)
    lists[remote] = open
    openFolderLists = lists
  }

  // A dropdown that opens below the fold is scrolled into view as it grows: just enough to show
  // all of it, and never so far that its header leaves the top. Scrolling by hand lets go.
  property Item revealing: null

  function reveal(item) {
    revealing = item
    revealTimer.restart()
    Qt.callLater(keepRevealed)
  }

  function keepRevealed() {
    if (!revealing || !revealing.visible) return
    var f = drivesFlick
    var margin = Style.space(8)
    var top = revealing.mapToItem(f.contentItem, 0, 0).y
    var bottom = top + revealing.height
    var y = f.contentY
    if (bottom + margin > y + f.height) y = bottom + margin - f.height
    if (top - margin < y) y = top - margin
    f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), y))
  }

  // Long enough to follow a dropdown's 140 ms opening, and the panel growing with it
  Timer {
    id: revealTimer
    interval: 400
    onTriggered: {
      root.keepRevealed()
      root.revealing = null
    }
  }

  Connections {
    target: drivesFlick
    function onContentHeightChanged() { root.keepRevealed() }
    function onHeightChanged() { root.keepRevealed() }
    function onMovementStarted() { root.revealing = null }
  }

  function showView(view) {
    // An error belongs to the view it happened in
    service.clearError()
    if (view === "settings") settingsView.load()
    if (view === "add") addForm.reset()
    if (view === "log") logView.load("")
    currentView = view
    panelFlick.contentY = 0
  }

  function openBrowse(remote) {
    service.clearError()
    var d = service.driveByName(remote)
    picker.open(remote, d ? d.label : remote)
    currentView = "browse"
    panelFlick.contentY = 0
  }

  function openClient(remote) {
    service.clearError()
    var d = service.driveByName(remote)
    clientForm.open(remote, d ? d.provider : "")
    currentView = "client"
    panelFlick.contentY = 0
  }

  function addProvider(provId) {
    showView("add")
    addForm.selectProvider(provId)
  }

  function resetScroll() {
    drivesFlick.contentY = 0
  }

  function forceLast() {
    var last = service.lastErrorArgs
    if (!last) return
    var args = JSON.parse(JSON.stringify(last.args || {}))
    args.force = true
    service.act(last.cmd, args, "Disconnecting…")
  }

  // ================================================================ drives
  Item {
    anchors.fill: parent
    visible: root.currentView === "drives"

    Flickable {
      id: drivesFlick
      anchors {
        left: parent.left
        right: parent.right
        top: parent.top
        bottom: footer.top
        bottomMargin: root.footerGap
      }
      contentWidth: width
      contentHeight: drivesCol.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      ColumnLayout {
        id: drivesCol
        width: drivesFlick.width
        spacing: Style.space(18)

        PanelHero {
          id: hero
          Layout.fillWidth: true
          title: "Cloud Drives"
          meta: root.headline
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            CloudIcon {
              iconSize: Style.font.display
              color: root.statusColor
              fontFamily: root.fontFamily
              active: root.service.statusState === "online"
              busy: root.service.busy
              attention: root.service.attention
            }
          }

          trailingControl: Component {
            ToggleSwitch {
              id: allSwitch
              visible: root.service.hasDrives && root.service.ready
              checked: root.service.allStreaming
              foreground: hero.foreground
              onToggled: root.service.streamAll(!root.service.allStreaming)

              PanelToolTip {
                visible: allSwitch.containsMouse
                text: root.service.allStreaming ? "Stop streaming every drive (M)" : "Stream every drive (M)"
                fontFamily: hero.fontFamily
              }
            }
          }
        }

        // Status line: progress, results and errors, with a way forward when there is one
        RowLayout {
          visible: root.service.lastAction !== "" || root.service.lastError !== ""
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: root.service.lastAction !== "" ? root.service.lastAction : root.service.lastError
            color: root.service.lastError !== "" && root.service.lastAction === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            maximumLineCount: 4
            elide: Text.ElideRight
          }

          Button {
            visible: root.canForce && root.service.lastAction === ""
            text: "Unmount anyway"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.urgent
            bordered: true
            onClicked: root.forceLast()
          }

          PanelActionButton {
            visible: root.service.lastError !== "" && root.service.lastAction === ""
            iconText: "󰅖"
            tooltipText: "Dismiss"
            foreground: root.dim
            hoverColor: root.urgent
            onClicked: root.service.clearError()
          }
        }

        // Background service unreachable, or rclone in trouble
        BorderSurface {
          visible: (root.service.everConnected && !root.service.connected) || root.service.startError !== ""
                   || (root.service.connected && (root.service.engine.state === "missing" || root.service.engine.state === "error"))
          Layout.fillWidth: true
          implicitHeight: engineCol.implicitHeight + Style.space(16)
          radius: Style.cornerRadius
          color: Qt.alpha(root.urgent, 0.08)
          borderSpec: Border.controlSpec("normal", root.urgent, root.urgent)

          ColumnLayout {
            id: engineCol
            anchors {
              fill: parent
              margins: Style.space(8)
            }
            spacing: Style.space(8)

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: !root.service.connected
                    ? (root.service.startError !== "" ? root.service.startError : "Lost the background service; reconnecting…")
                    : (root.service.engine.error || "rclone is not running")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Button {
              text: root.service.connected ? "Restart rclone" : "Start it"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              bordered: true
              onClicked: root.service.connected ? root.service.restartEngine() : root.service.ensureDaemon()
            }
          }
        }

        Repeater {
          // Bound by index: each state push assigns new objects, and a model of the array itself
          // would rebuild every card, closing open editors and confirmations
          model: root.service.drives.length

          DriveRow {
            required property int index
            Layout.fillWidth: true
            drive: root.service.drives[index] || ({})
            service: root.service
            foreground: root.foreground
            fontFamily: root.fontFamily
            home: root.service.home
            mountRoot: root.service.mountRoot
            foldersOpen: root.openFolderLists[drive.name] === true
            onFoldersToggled: {
              var open = !foldersOpen
              root.setFoldersOpen(drive.name, open)
              if (open) root.reveal(folderList)
            }
            // The folder about to be added shows when the panel comes back
            onKeepFolderRequested: function(remote) {
              root.setFoldersOpen(remote, true)
              root.openBrowse(remote)
            }
            onClientIdRequested: function(remote) { root.openClient(remote) }
          }
        }

        EmptyState {
          visible: root.service.ready && !root.service.hasDrives
          Layout.fillWidth: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          onAddProvider: function(provId) { root.addProvider(provId) }
        }

        RecentFiles {
          id: recentFiles
          Layout.fillWidth: true
          onExpandedChanged: if (expanded) root.reveal(recentFiles)
          files: root.service.recent
          foreground: root.foreground
          fontFamily: root.fontFamily
          onFileSelected: function(path) { root.service.openPath(path) }
        }
      }
    }

    // Pinned under the list: adding a drive, and the panel-wide actions
    ColumnLayout {
      id: footer
      anchors {
        left: parent.left
        right: parent.right
        bottom: parent.bottom
      }
      spacing: Style.space(8)

      PanelSeparator {
        Layout.fillWidth: true
        foreground: root.foreground
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(4)

        Button {
          iconText: "󰐕"
          text: "Add drive"
          tooltipText: "Connect a cloud drive (A)"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.foreground
          bordered: false
          onClicked: root.showView("add")
        }

        Item { Layout.fillWidth: true }

        PanelActionButton {
          visible: root.summary.folders > 0
          iconText: "󰑐"
          tooltipText: "Sync local folders now (R)"
          foreground: root.dim
          hoverColor: Color.accent
          onClicked: root.service.syncNow("")
        }

        PanelActionButton {
          iconText: "󰉋"
          tooltipText: "Open " + Model.tildify(root.service.cloudRoot, root.service.home) + " (O)"
          foreground: root.dim
          hoverColor: Color.accent
          onClicked: root.service.openPath(root.service.cloudRoot)
        }

        PanelActionButton {
          iconText: "󰒓"
          tooltipText: "Settings (S)"
          foreground: root.dim
          hoverColor: Color.accent
          onClicked: root.showView("settings")
        }
      }
    }
  }

  // ================================================================ other views
  Flickable {
    id: panelFlick
    anchors.fill: parent
    visible: root.currentView !== "drives"
    contentWidth: width
    contentHeight: viewCol.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    interactive: contentHeight > height
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    Column {
      id: viewCol
      width: panelFlick.width
      spacing: Style.space(10)

      AddAccountForm {
        id: addForm
        visible: root.currentView === "add"
        width: parent.width
        backend: root.service
        foreground: root.foreground
        urgent: root.urgent
        fontFamily: root.fontFamily
        onAccountAdded: function(name) { root.showView("drives") }
        onCancelled: root.showView("drives")
      }

      SettingsView {
        id: settingsView
        visible: root.currentView === "settings"
        width: parent.width
        service: root.service
        foreground: root.foreground
        fontFamily: root.fontFamily
        onBack: root.showView("drives")
        onLogRequested: root.showView("log")
      }

      LogView {
        id: logView
        visible: root.currentView === "log"
        width: parent.width
        service: root.service
        foreground: root.foreground
        fontFamily: root.fontFamily
        onBack: root.showView("settings")
      }

      FolderPicker {
        id: picker
        visible: root.currentView === "browse"
        width: parent.width
        service: root.service
        foreground: root.foreground
        fontFamily: root.fontFamily
        home: root.service.home
        onDone: root.showView("drives")
        onCancelled: root.showView("drives")
      }

      ClientIdForm {
        id: clientForm
        visible: root.currentView === "client"
        width: parent.width
        service: root.service
        foreground: root.foreground
        fontFamily: root.fontFamily
        onDone: root.showView("drives")
        onCancelled: root.showView("drives")
      }

      // Results of actions started from these views
      Text {
        visible: root.currentView !== "drives" && (root.service.lastAction !== ""
                 || (root.service.lastError !== "" && (root.currentView === "browse" || root.currentView === "settings")))
        width: parent.width
        textFormat: Text.PlainText
        text: root.service.lastAction !== "" ? root.service.lastAction : root.service.lastError
        color: root.service.lastError !== "" && root.service.lastAction === "" ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }
}
