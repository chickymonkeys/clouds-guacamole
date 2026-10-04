import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// A cloud drive: streaming switch, quota, problems with a one-click fix, and a dropdown of the
// folders of this drive that are kept on this device. Renaming, moving, the own client and
// removing live behind the ⋯ button, so the card itself only says what matters now.
Item {
  id: root

  property var drive: ({})
  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string home: ""
  property string mountRoot: ""
  property bool editing: false
  property bool confirmingRemove: false
  // Whether the local folders are listed; the panel keeps it, so it outlives this card
  property bool foldersOpen: false

  signal keepFolderRequested(string remote)
  signal clientIdRequested(string remote)
  signal foldersToggled()

  readonly property color dim: Qt.darker(foreground, 1.6)
  // What the panel scrolls into view when the folders open
  readonly property Item folderList: folderListItem
  // Everything below the header lines up with the drive's name
  readonly property real badgeSize: Style.space(30)
  readonly property real indent: badgeSize + Style.space(10)
  readonly property bool mounted: drive.mountState === "mounted"
  readonly property bool transitioning: drive.mountState === "mounting" || drive.mountState === "unmounting"
  readonly property bool failed: drive.mountState === "error" || drive.mountState === "foreign"
  readonly property bool quotaKnown: drive.quotaKnown === true
  readonly property bool quotaHigh: quotaKnown && drive.quotaPercent > 90
  readonly property var folders: drive.folders || []
  readonly property bool hasEdits: editing && (labelInput.text.trim() !== String(drive.label || "")
                                              || pathInput.text.trim() !== String(drive.mountPath || ""))

  // One line under the name. The switch already says a drive is streaming, so the state is
  // only spelled out when it isn't, or when there is nothing else to say.
  readonly property string subtitle: {
    var parts = []
    if (!mounted || (!quotaKnown && !(drive.uploads > 0)))
      parts.push((failed ? "󰀦 " : "") + Model.mountStateText(drive))
    if (quotaKnown) parts.push(Model.formatBytes(drive.quotaUsed) + " of " + Model.formatBytes(drive.quotaTotal))
    if (drive.uploads > 0) parts.push("󰕒 " + drive.uploads + " uploading")
    return parts.join("  ·  ")
  }
  readonly property color subtitleColor: {
    if (failed) return Color.urgent
    if (drive.uploads > 0) return Color.accent
    if (transitioning || drive.mountState === "waiting") return Qt.darker(foreground, 1.2)
    return dim
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

  implicitHeight: column.implicitHeight

  ColumnLayout {
    id: column
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
    }
    spacing: Style.space(8)

    // Header: provider badge, name with one status line and the quota, then the actions
    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(10)

      Item {
        implicitWidth: root.badgeSize
        implicitHeight: root.badgeSize

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
        Layout.alignment: Qt.AlignVCenter
        spacing: Style.space(2)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.drive.label || root.drive.name || ""
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          color: root.foreground
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.subtitle
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: root.subtitleColor
          elide: Text.ElideRight
        }

        Rectangle {
          visible: root.quotaKnown
          Layout.fillWidth: true
          Layout.topMargin: Style.space(3)
          implicitHeight: Style.space(3)
          radius: height / 2
          color: Qt.rgba(1, 1, 1, 0.08)

          Rectangle {
            width: Math.min(parent.width, Math.max(0, parent.width * (root.drive.quotaPercent || 0) / 100))
            height: parent.height
            radius: height / 2
            color: root.quotaHigh ? Color.urgent : (root.mounted ? Color.accent : root.dim)
          }

          HoverHandler { id: quotaHover }

          PanelToolTip {
            visible: quotaHover.hovered
            text: root.drive.quotaPercent + "% used, " + Model.formatBytes(root.drive.quotaFree) + " free"
            fontFamily: root.fontFamily
          }
        }
      }

      // Kept in the layout while hidden, so every card's switch sits in the same column
      PanelActionButton {
        iconText: "󰉋"
        tooltipText: "Open " + Model.tildify(root.drive.mountPath, root.home)
        foreground: root.dim
        hoverColor: Color.accent
        enabled: root.mounted
        opacity: root.mounted ? 1 : 0
        onClicked: root.service.openPath(root.drive.mountPath)
      }

      PanelActionButton {
        iconText: "󰇘"
        tooltipText: root.editing ? "Close" : "Rename, move or remove"
        foreground: root.editing ? Color.accent : root.dim
        hoverColor: Color.accent
        onClicked: {
          if (!root.editing) root.resetEditor()
          root.confirmingRemove = false
          root.editing = !root.editing
        }
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
    }

    // Drawer: display name, mount location, own client, and removing the drive
    BorderSurface {
      visible: root.editing
      Layout.fillWidth: true
      implicitHeight: editCol.implicitHeight + Style.space(20)
      radius: Style.cornerRadius
      color: Qt.rgba(1, 1, 1, 0.04)
      borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

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
          font.pixelSize: Style.font.caption
          color: Qt.darker(root.foreground, 1.4)
        }

        TextField {
          id: labelInput
          Layout.fillWidth: true
          placeholderText: root.drive.name || ""
          onAccepted: if (root.hasEdits) root.applyEdits()
        }

        Text {
          Layout.topMargin: Style.space(4)
          text: "Mount location"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
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
          font.pixelSize: Style.font.caption - Style.space(1)
          color: root.dim
          wrapMode: Text.WordWrap
        }

        RowLayout {
          Layout.fillWidth: true
          Layout.topMargin: Style.space(4)
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

        PanelSeparator {
          Layout.fillWidth: true
          Layout.topMargin: Style.space(4)
          foreground: root.foreground
        }

        RowLayout {
          visible: !root.confirmingRemove
          Layout.fillWidth: true
          spacing: Style.space(8)

          Button {
            visible: root.drive.canCustomClient === true
            iconText: "󰌋"
            text: root.drive.customClient ? "Change my client" : "Set up my own client"
            tooltipText: "A client of your own isn't shared with every rclone user, so it isn't rate-limited with them"
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
            iconText: "󰆴"
            text: "Remove drive"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: Color.urgent
            bordered: false
            onClicked: root.confirmingRemove = true
          }
        }

        // Remove confirmation
        ColumnLayout {
          visible: root.confirmingRemove
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: "Remove " + (root.drive.label || root.drive.name) + "? Its rclone account is deleted; local copies stay."
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            color: Color.urgent
            wrapMode: Text.WordWrap
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Button {
              text: "Remove"
              iconText: "󰆴"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: Color.urgent
              bordered: true
              onClicked: {
                root.confirmingRemove = false
                root.editing = false
                root.service.removeRemote(root.drive.name)
              }
            }

            Button {
              text: "Keep it"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              bordered: false
              onClicked: root.confirmingRemove = false
            }
          }
        }
      }
    }

    // Mount problem with its fix
    BorderSurface {
      visible: (root.drive.error || "") !== "" && root.drive.mountState !== "mounted"
      Layout.fillWidth: true
      implicitHeight: errCol.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.alpha(root.drive.mountState === "waiting" ? root.foreground : Color.urgent, 0.06)
      borderSpec: Border.controlSpec("normal", root.drive.mountState === "waiting" ? root.foreground : Color.urgent, Color.urgent)

      ColumnLayout {
        id: errCol
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        spacing: Style.space(8)

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
        Layout.leftMargin: root.indent
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
          textFormat: Text.PlainText
          text: "󰀦  " + modelData.text
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
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

    // Folders kept on this device, lined up under the drive's name
    FolderList {
      id: folderListItem
      Layout.fillWidth: true
      Layout.leftMargin: root.indent - Style.space(8)
      folders: root.folders
      open: root.foldersOpen
      service: root.service
      foreground: root.foreground
      fontFamily: root.fontFamily
      home: root.home
      onToggled: root.foldersToggled()
      onKeepFolderRequested: root.keepFolderRequested(root.drive.name)
    }
  }
}
