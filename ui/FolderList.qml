import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// A drive's local folders as a dropdown: one line saying how many there are and what they are
// doing, which opens into the list. Folders that need a look stay listed while it's closed, so
// nothing that needs you is ever behind a click.
Item {
  id: root

  property var folders: []
  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string home: ""
  property bool open: false

  signal toggled()
  signal keepFolderRequested()

  readonly property color dim: Qt.darker(foreground, 1.6)
  // The header's glyph column; the folders hang under its label, along a guide from the glyph
  readonly property real glyphWidth: Style.space(16)
  readonly property real childIndent: glyphWidth + Style.space(8)
  readonly property var group: Model.folderGroup(folders)
  readonly property bool busy: !open && group.busy
  readonly property string names: {
    var shown = folders.slice(0, 8).map(function(f) { return f.name || "/" })
    var rest = folders.length - shown.length
    return shown.join(", ") + (rest > 0 ? " and " + rest + " more" : "")
  }

  implicitHeight: col.implicitHeight

  // Ties the listed folders to the header, open or closed
  Rectangle {
    readonly property real start: header.visible ? header.height : 0
    visible: header.visible && col.implicitHeight - start > Style.space(12)
    x: Style.space(8) + root.glyphWidth / 2 - width / 2
    y: start
    width: Math.max(1, Style.space(1))
    height: col.implicitHeight - start - Style.space(8)
    color: Qt.alpha(root.foreground, 0.1)
  }

  ColumnLayout {
    id: col
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
    }
    spacing: 0

    CursorSurface {
      id: header
      visible: root.folders.length > 0
      Layout.fillWidth: true
      implicitHeight: Style.space(28)
      foreground: root.foreground
      hasCursor: headerMouse.containsMouse

      MouseArea {
        id: headerMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggled()
      }

      RowLayout {
        // The same right edge as the folders' state
        anchors {
          fill: parent
          leftMargin: Style.space(8)
          rightMargin: Style.space(4)
        }
        spacing: Style.space(8)

        Item {
          implicitWidth: root.glyphWidth
          implicitHeight: stack.implicitHeight

          Text {
            id: stack
            visible: !root.busy
            anchors.centerIn: parent
            text: "󰉓"
            color: Qt.darker(root.foreground, 1.35)
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Text {
            visible: root.busy
            anchors.centerIn: parent
            text: "󰑐"
            color: Color.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            RotationAnimation on rotation {
              running: root.busy
              from: 0
              to: 360
              duration: 1100
              loops: Animation.Infinite
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          text: "Local folders"
          color: headerMouse.containsMouse ? Qt.lighter(root.foreground, 1.4) : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Rectangle {
          Layout.alignment: Qt.AlignVCenter
          implicitWidth: Math.max(implicitHeight, countText.implicitWidth + Style.space(12))
          implicitHeight: countText.implicitHeight + Style.space(4)
          radius: height / 2
          color: Qt.alpha(root.foreground, 0.08)

          Text {
            id: countText
            anchors.centerIn: parent
            text: String(root.folders.length)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
            color: Qt.darker(root.foreground, 1.3)
          }
        }

        Item { Layout.fillWidth: true }

        Text {
          textFormat: Text.PlainText
          text: root.group.text
          color: root.group.tone === "urgent" ? Color.urgent : (root.group.tone === "accent" ? Color.accent : root.dim)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          text: "󰅀"
          rotation: root.open ? 0 : -90
          color: headerMouse.containsMouse ? Color.accent : Qt.darker(root.foreground, 1.2)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          Behavior on rotation { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
        }
      }

      PanelToolTip {
        visible: headerMouse.containsMouse && !root.open && root.names !== ""
        text: root.names
        fontFamily: root.fontFamily
      }
    }

    Repeater {
      // By index: each state push brings new objects, and rebuilding rows would drop open confirmations
      model: root.folders.length

      FolderRow {
        id: row
        required property int index
        // Kept in place while closed when it needs a look, so the list never reorders
        readonly property bool shown: root.open || Model.folderStaysListed(folder)
        Layout.fillWidth: true
        Layout.leftMargin: root.childIndent
        Layout.preferredHeight: shown ? implicitHeight : 0
        visible: Layout.preferredHeight > 0.5
        opacity: shown ? 1 : 0
        clip: true
        folder: root.folders[index] || ({})
        service: root.service
        foreground: root.foreground
        fontFamily: root.fontFamily
        home: root.home
        Behavior on Layout.preferredHeight { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
        Behavior on opacity { NumberAnimation { duration: 140 } }
      }
    }

    // At the end of the open list, or on its own when the drive keeps no folders yet
    Item {
      readonly property bool shown: root.open || root.folders.length === 0
      Layout.fillWidth: true
      Layout.leftMargin: root.folders.length > 0 ? root.childIndent : 0
      Layout.preferredHeight: shown ? keepButton.implicitHeight : 0
      implicitHeight: keepButton.implicitHeight
      visible: Layout.preferredHeight > 0.5
      opacity: shown ? 1 : 0
      clip: true
      Behavior on Layout.preferredHeight { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
      Behavior on opacity { NumberAnimation { duration: 140 } }

      Button {
        id: keepButton
        horizontalPadding: Style.space(8)
        iconText: "󰐕"
        iconSize: Style.font.body
        text: "Keep a folder on this device"
        tooltipText: "Pick a cloud folder to have locally: instant and available offline, synced both ways"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: Qt.darker(root.foreground, 1.45)
        bordered: false
        onClicked: root.keepFolderRequested()
      }
    }
  }
}
