import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

Item {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  signal back()
  signal logRequested()

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property var s: service ? service.appSettings : ({})

  implicitHeight: col.implicitHeight

  function load() {
    mountRootField.text = String(s.mount_root || "")
    localRootField.text = String(s.local_root || "")
    cacheSizeField.value = Number(s.cache_max_size_gb || 20)
    intervalField.value = Number(s.sync_interval_min || 5)
    backupField.value = Number(s.backup_days || 30)
  }

  component Label: Text {
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    color: root.foreground
  }

  component Hint: Text {
    Layout.fillWidth: true
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption - Style.space(2)
    color: root.dim
    wrapMode: Text.WordWrap
  }

  // An engine action: icon over a short label, so four of them share one row at panel width.
  // Same API and state colors as the kit's Button.
  component EngineAction: BorderSurface {
    id: tile

    property string text: ""
    property string iconText: ""
    property string tooltipText: ""
    signal clicked()

    Layout.fillWidth: true
    Layout.preferredWidth: 1
    implicitWidth: tileCol.implicitWidth + Style.space(12)
    implicitHeight: tileCol.implicitHeight + Style.space(16)
    radius: Style.cornerRadius
    opacity: enabled ? 1 : 0.45
    color: tileMouse.pressed ? Style.pressedFillFor(root.foreground, Color.accent)
         : tileMouse.containsMouse ? Style.hoverFillFor(root.foreground, Color.accent) : "transparent"
    borderSpec: Border.controlSpec(tileMouse.containsMouse ? "hover-cursor" : "normal", root.foreground, Color.accent)

    Column {
      id: tileCol
      anchors.centerIn: parent
      spacing: Style.space(4)

      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: tile.iconText
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        color: tileMouse.containsMouse ? Color.accent : root.foreground
      }

      Text {
        anchors.horizontalCenter: parent.horizontalCenter
        textFormat: Text.PlainText
        text: tile.text
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        color: root.foreground
      }
    }

    MouseArea {
      id: tileMouse
      anchors.fill: parent
      enabled: tile.enabled
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: tile.clicked()
    }

    PanelToolTip {
      visible: tileMouse.containsMouse && tile.tooltipText !== ""
      text: tile.tooltipText
      fontFamily: root.fontFamily
    }
  }

  ColumnLayout {
    id: col
    width: parent.width
    spacing: Style.space(12)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Button {
        iconText: "󰁝"
        text: "Back"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        onClicked: root.back()
      }

      Text {
        Layout.fillWidth: true
        text: "Settings"
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        color: root.foreground
      }
    }

    BorderSurface {
      Layout.fillWidth: true
      implicitHeight: cfgCol.implicitHeight + Style.space(20)
      radius: Style.cornerRadius
      color: Qt.rgba(1, 1, 1, 0.03)
      borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

      ColumnLayout {
        id: cfgCol
        anchors {
          fill: parent
          margins: Style.space(10)
        }
        spacing: Style.space(10)

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(4)
          Label { text: "Stream drives in" }
          RowLayout {
            Layout.fillWidth: true
            TextField {
              id: mountRootField
              Layout.fillWidth: true
              placeholderText: "~/Cloud/Stream"
              onAccepted: root.service.setSetting("mount_root", text.trim())
            }
            Button {
              text: "Save"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              bordered: true
              onClicked: root.service.setSetting("mount_root", mountRootField.text.trim())
            }
          }
          Hint { text: "Each drive appears here as a folder, streamed on demand: files download when you open them, and only those are cached." }
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.space(4)
          Label { text: "Sync folders kept on this device in" }
          RowLayout {
            Layout.fillWidth: true
            TextField {
              id: localRootField
              Layout.fillWidth: true
              placeholderText: "~/Cloud/Sync"
              onAccepted: root.service.setSetting("local_root", text.trim())
            }
            Button {
              text: "Save"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              bordered: true
              onClicked: root.service.setSetting("local_root", localRootField.text.trim())
            }
          }
          Hint { text: "Where folders you keep on this device go by default, synced both ways. Each one can also go anywhere you like." }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            Label { text: "Stream cache size (GB)" }
            Hint { text: "Files you open stay cached for instant reopening; the least recently used go first." }
          }
          NumberField {
            id: cacheSizeField
            from: 1
            to: 4000
            foreground: root.foreground
            fontFamily: root.fontFamily
            onModified: function(v) { root.service.setSetting("cache_max_size_gb", v) }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            Label { text: "Check the cloud every (min)" }
            Hint { text: "Local edits sync within seconds; this is how often cloud-side changes are picked up." }
          }
          NumberField {
            id: intervalField
            from: 1
            to: 240
            foreground: root.foreground
            fontFamily: root.fontFamily
            onModified: function(v) { root.service.setSetting("sync_interval_min", v) }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            Label { text: "Keep replaced local files (days)" }
            Hint { text: "Local files a sync overwrote or deleted are moved aside, not lost." }
          }
          NumberField {
            id: backupField
            from: 1
            to: 365
            foreground: root.foreground
            fontFamily: root.fontFamily
            onModified: function(v) { root.service.setSetting("backup_days", v) }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)
          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            Label { text: "Notifications" }
            Hint { text: "Conflicts and problems that need you." }
          }
          ToggleSwitch {
            checked: root.s.notifications === true
            foreground: root.foreground
            onToggled: root.service.setSetting("notifications", !(root.s.notifications === true))
          }
        }
      }
    }

    BorderSurface {
      Layout.fillWidth: true
      implicitHeight: engCol.implicitHeight + Style.space(20)
      radius: Style.cornerRadius
      color: Qt.rgba(1, 1, 1, 0.03)
      borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

      ColumnLayout {
        id: engCol
        anchors {
          fill: parent
          margins: Style.space(10)
        }
        spacing: Style.space(6)

        Label { text: "Engine" }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.service.connected
                ? ("Guacamole " + root.service.daemonVersion + " · " + (root.service.engine.version || "rclone") + " · " + root.service.engine.state)
                : "Background service not connected"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: root.service.ready ? Color.accent : Color.urgent
          wrapMode: Text.WordWrap
        }

        RowLayout {
          Layout.fillWidth: true
          Layout.topMargin: Style.space(2)
          spacing: Style.space(6)

          EngineAction {
            text: "Restart"
            iconText: "󰑐"
            tooltipText: "Restart rclone: drives mount again and syncs resume"
            enabled: root.service.connected
            onClicked: root.service.restartEngine()
          }

          EngineAction {
            text: "Log"
            iconText: "󰈙"
            tooltipText: "What Guacamole and rclone did recently"
            enabled: root.service.connected
            onClicked: root.logRequested()
          }

          EngineAction {
            text: "Filters"
            iconText: "󰈲"
            tooltipText: "Files that are never synced, in your editor. Changing them makes the next syncs safe resyncs."
            enabled: (root.service.statePaths.filters || "") !== ""
            onClicked: root.service.openPath(root.service.statePaths.filters)
          }

          EngineAction {
            text: "Backups"
            iconText: "󰁯"
            tooltipText: root.service.backupFiles > 0
                         ? root.service.backupFiles + " local files that syncs replaced or deleted, kept " + (root.s.backup_days || 30) + " days"
                         : "Local files that syncs replace or delete are kept here for " + (root.s.backup_days || 30) + " days"
            enabled: (root.service.statePaths.backups || "") !== ""
            onClicked: {
              if (root.service.backupFiles > 0) root.service.openPath(root.service.statePaths.backups)
              else root.service.notice("No local file has been replaced yet. When a sync overwrites or deletes one, the old copy is kept here for "
                                       + (root.s.backup_days || 30) + " days.")
            }
          }
        }
      }
    }
  }
}
