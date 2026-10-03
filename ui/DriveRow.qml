import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// A cloud drive: streaming switch, quota, problems with a one-click fix, and the folders of
// this drive that are kept on this device.
CursorSurface {
  id: root

  property var drive: ({})
  property var service: null
  property string fontFamily: Style.font.family
  property string home: ""
  property string mountRoot: ""
  property bool editing: false
  property bool confirmingRemove: false

  signal keepFolderRequested(string remote)
  signal clientIdRequested(string remote)

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property bool mounted: drive.mountState === "mounted"
  readonly property bool transitioning: drive.mountState === "mounting" || drive.mountState === "unmounting"
  readonly property var folders: drive.folders || []
  readonly property bool hasEdits: editing && (labelInput.text.trim() !== String(drive.label || "")
                                              || pathInput.text.trim() !== String(drive.mountPath || ""))
  readonly property color chipColor: {
    switch (drive.mountState) {
      case "mounted": return Color.accent
      case "error":
      case "foreign": return Color.urgent
      case "waiting":
      case "mounting":
      case "unmounting": return Qt.darker(foreground, 1.2)
      default: return Qt.darker(foreground, 1.8)
    }
  }

  function resetEditor() {
    labelInput.text = String(drive.label || drive.name || "")
    pathInput.text = String(drive.mountPath || "")
  }

  function applyEdits() {
    var label = labelInput.text.trim()
    var path = pathInput.text.trim()
    var defaultPath = root.mountRoot + "/" + drive.name
    root.service.setDrive(drive.name,
                          label !== String(drive.label) ? label : null,
                          path !== String(drive.mountPath) ? (path === defaultPath ? "" : path) : null)
    root.editing = false
  }

  implicitHeight: column.implicitHeight + Style.space(16)
  hasCursor: false

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.NoButton
    onEntered: root.hasCursor = true
    onExited: root.hasCursor = false
  }

  ColumnLayout {
    id: column
    anchors {
      left: parent.left
      right: parent.right
      verticalCenter: parent.verticalCenter
      leftMargin: Style.space(10)
      rightMargin: Style.space(10)
    }
    spacing: Style.space(6)

    // Header: provider badge, name, state, actions
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Item {
        implicitWidth: Style.space(30)
        implicitHeight: Style.space(30)

        Rectangle {
          anchors.fill: parent
          radius: Style.cornerRadius
          color: root.drive.color ? Qt.alpha(root.drive.color, 0.15) : Qt.rgba(1, 1, 1, 0.08)
        }

        Text {
          anchors.centerIn: parent
          text: root.drive.glyph || "󰅟"
          color: root.drive.color || root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.icon
        }
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            textFormat: Text.PlainText
            text: root.drive.label || root.drive.name || ""
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            color: root.foreground
            elide: Text.ElideRight
            Layout.maximumWidth: Style.space(130)
          }

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: (root.mounted ? "󰄬 " : (root.drive.mountState === "error" || root.drive.mountState === "foreign" ? "󰀦 " : ""))
                  + Model.mountStateText(root.drive)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
            color: root.chipColor
            elide: Text.ElideRight
          }
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: Model.tildify(root.drive.mountPath, root.home)
                + (root.drive.uploads > 0 ? "  ·  󰕒 " + root.drive.uploads + " uploading" : "")
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.drive.uploads > 0 ? Color.accent : root.dim
          elide: Text.ElideMiddle
        }
      }

      PanelActionButton {
        iconText: "󰏫"
        tooltipText: root.editing ? "Close editor" : "Rename or move this drive"
        foreground: root.editing ? Color.accent : root.foreground
        hoverColor: Color.accent
        onClicked: {
          if (!root.editing) root.resetEditor()
          root.editing = !root.editing
        }
      }

      PanelActionButton {
        visible: root.mounted
        iconText: "󰉋"
        tooltipText: "Open in the file manager"
        foreground: root.foreground
        hoverColor: Color.accent
        onClicked: root.service.openPath(root.drive.mountPath)
      }

      ToggleSwitch {
        id: streamSwitch
        checked: root.drive.stream === true
        busy: root.transitioning
        foreground: root.foreground
        onToggled: root.service.setStream(root.drive.name, !(root.drive.stream === true), false)

        PanelToolTip {
          visible: streamSwitch.containsMouse
          text: root.drive.stream ? "Stop streaming (unmount)" : "Stream this drive (mount)"
          fontFamily: root.fontFamily
        }
      }

      PanelActionButton {
        iconText: "󰆴"
        tooltipText: "Remove this account"
        foreground: root.confirmingRemove ? Color.urgent : root.dim
        hoverColor: Color.urgent
        onClicked: root.confirmingRemove = !root.confirmingRemove
      }
    }

    // Remove confirmation
    RowLayout {
      visible: root.confirmingRemove
      Layout.fillWidth: true
      spacing: Style.space(8)

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: "Remove " + (root.drive.label || root.drive.name) + "? Its rclone account is deleted; local copies stay."
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        color: Color.urgent
        wrapMode: Text.WordWrap
      }

      Button {
        text: "Remove"
        iconText: "󰆴"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: Color.urgent
        bordered: true
        onClicked: {
          root.confirmingRemove = false
          root.service.removeRemote(root.drive.name)
        }
      }

      Button {
        text: "Cancel"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: false
        onClicked: root.confirmingRemove = false
      }
    }

    // Quota
    ColumnLayout {
      visible: root.drive.quotaKnown === true
      Layout.fillWidth: true
      spacing: Style.space(3)

      RowLayout {
        Layout.fillWidth: true

        Text {
          text: Model.formatBytes(root.drive.quotaUsed) + " of " + Model.formatBytes(root.drive.quotaTotal)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.dim
        }

        Item { Layout.fillWidth: true }

        Text {
          text: root.drive.quotaPercent + "%"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          font.bold: true
          color: root.drive.quotaPercent > 90 ? Color.urgent : Qt.darker(root.foreground, 1.4)
        }
      }

      Rectangle {
        Layout.fillWidth: true
        implicitHeight: Style.space(3)
        radius: height / 2
        color: Qt.rgba(1, 1, 1, 0.08)

        Rectangle {
          width: Math.min(parent.width, Math.max(0, parent.width * (root.drive.quotaPercent || 0) / 100))
          height: parent.height
          radius: height / 2
          color: root.drive.quotaPercent > 90 ? Color.urgent : Color.accent
        }
      }
    }

    // Mount problem with its fix
    BorderSurface {
      visible: (root.drive.error || "") !== "" && root.drive.mountState !== "mounted"
      Layout.fillWidth: true
      implicitHeight: errCol.implicitHeight + Style.space(12)
      radius: Style.cornerRadius
      color: Qt.alpha(root.drive.mountState === "waiting" ? root.foreground : Color.urgent, 0.06)
      borderSpec: Border.controlSpec("normal", root.drive.mountState === "waiting" ? root.foreground : Color.urgent, Color.urgent)

      ColumnLayout {
        id: errCol
        anchors {
          fill: parent
          margins: Style.space(6)
        }
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.drive.error + (root.drive.retrying ? " Retrying automatically." : "")
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: root.foreground
          wrapMode: Text.WordWrap
          maximumLineCount: 4
          elide: Text.ElideRight
        }

        Flow {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Button {
            visible: root.drive.mountState === "foreign"
            text: "Take over"
            tooltipText: "Unmount the other program's mount and stream it from here"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.takeOver(root.drive.name)
          }

          Button {
            visible: root.drive.errorKind === "auth"
            text: "Reconnect account"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.reconnect(root.drive.name)
          }

          Button {
            visible: root.drive.errorKind === "rate" && root.drive.canCustomClient === true && !root.drive.customClient
            text: "Set up my own client"
            iconText: "󰌋"
            tooltipText: "A client of your own isn't shared with every rclone user, so it isn't rate-limited with them"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.clientIdRequested(root.drive.name)
          }

          Button {
            visible: root.drive.mountState === "error" || root.drive.mountState === "waiting"
            text: "Retry now"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.retryDrive(root.drive.name)
          }
        }
      }
    }

    // rclone's shared OAuth client, for every provider that can use an own one (needed for
    // Google Drive; the others can hide the notice)
    Repeater {
      model: root.drive.warnings || []

      RowLayout {
        required property var modelData
        Layout.fillWidth: true
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
          textFormat: Text.PlainText
          text: "󰀦  " + modelData.text
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: Qt.lighter(Color.accent, 1.1)
          wrapMode: Text.WordWrap
        }

        Button {
          visible: modelData.code === "shared_client_id" || modelData.code === "shared_app"
          Layout.alignment: Qt.AlignVCenter
          text: "Set up"
          iconText: "󰌋"
          tooltipText: "Create your own client, step by step, and sign in with it"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.foreground
          bordered: true
          onClicked: root.clientIdRequested(root.drive.name)
        }

        PanelActionButton {
          visible: modelData.dismissible === true
          Layout.alignment: Qt.AlignVCenter
          iconText: "󰅖"
          tooltipText: "Don't suggest this again"
          fontSize: Style.font.caption
          foreground: root.dim
          hoverColor: root.foreground
          onClicked: root.service.dismissHint(root.drive.name, modelData.code)
        }
      }
    }

    // Inline editor: display name and mount location
    BorderSurface {
      visible: root.editing
      Layout.fillWidth: true
      implicitHeight: editCol.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.rgba(1, 1, 1, 0.04)
      borderSpec: Border.controlSpec("focus", root.foreground, Color.accent)

      ColumnLayout {
        id: editCol
        anchors {
          fill: parent
          margins: Style.space(10)
        }
        spacing: Style.space(6)

        Text {
          text: "Display name"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: Qt.darker(root.foreground, 1.4)
        }

        TextField {
          id: labelInput
          Layout.fillWidth: true
          placeholderText: root.drive.name || ""
          onAccepted: if (root.hasEdits) root.applyEdits()
        }

        Text {
          text: "Mount location"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: Qt.darker(root.foreground, 1.4)
        }

        TextField {
          id: pathInput
          Layout.fillWidth: true
          placeholderText: root.mountRoot + "/" + (root.drive.name || "")
          onAccepted: if (root.hasEdits) root.applyEdits()
        }

        Text {
          visible: root.mounted
          Layout.fillWidth: true
          text: "Changing the location unmounts the drive and mounts it again."
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.dim
          wrapMode: Text.WordWrap
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Button {
            text: "Save"
            iconText: "󰄬"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            enabled: root.hasEdits
            opacity: enabled ? 1.0 : 0.45
            onClicked: root.applyEdits()
          }

          Button {
            visible: root.drive.canCustomClient === true
            text: root.drive.customClient ? "Change my client" : "Set up my own client"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: false
            onClicked: {
              root.editing = false
              root.clientIdRequested(root.drive.name)
            }
          }

          Item { Layout.fillWidth: true }

          Button {
            text: "Cancel"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: false
            onClicked: root.editing = false
          }
        }
      }
    }

    // Folders kept on this device
    ColumnLayout {
      Layout.fillWidth: true
      spacing: Style.space(2)

      Text {
        visible: root.folders.length > 0
        text: "ON THIS DEVICE"
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(2)
        font.letterSpacing: 0.6
        color: root.dim
        topPadding: Style.space(2)
      }

      Repeater {
        // By index: each state push brings new objects, and rebuilding rows would drop open confirmations
        model: root.folders.length

        FolderRow {
          required property int index
          Layout.fillWidth: true
          folder: root.folders[index] || ({})
          service: root.service
          foreground: root.foreground
          fontFamily: root.fontFamily
          home: root.home
        }
      }

      Button {
        Layout.topMargin: Style.space(2)
        iconText: "󰋊"
        text: root.folders.length > 0 ? "Keep another folder on this device" : "Keep a folder on this device"
        tooltipText: "Pick a cloud folder to have locally: instant and available offline, synced both ways"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: Qt.darker(root.foreground, 1.2)
        bordered: false
        onClicked: root.keepFolderRequested(root.drive.name)
      }
    }
  }
}
