import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Stands in for a drive's folded folders: how many, what they are doing, and their names on
// hover. Click to list them all, and again to fold them.
CursorSurface {
  id: root

  property var folders: []
  property bool expanded: false
  // Some of the drive's folders stay listed above this line
  property bool more: false
  property string fontFamily: Style.font.family

  signal toggled()

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property bool busy: !expanded && folders.some(function(f) { return f.state === "syncing" })
  readonly property string names: {
    var shown = folders.slice(0, 8).map(function(f) { return f.name || "/" })
    var rest = folders.length - shown.length
    return shown.join(", ") + (rest > 0 ? " and " + rest + " more" : "")
  }

  implicitHeight: Style.space(28)
  hasCursor: mouse.containsMouse

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: root.toggled()
  }

  RowLayout {
    anchors {
      fill: parent
      leftMargin: Style.space(8)
      rightMargin: Style.space(8)
    }
    spacing: Style.space(8)

    Item {
      implicitWidth: Style.space(16)
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
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.expanded ? "Show fewer"
            : root.folders.length + (root.more ? " more" : "") + (root.folders.length === 1 ? " folder" : " folders")
      color: Qt.darker(root.foreground, 1.15)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }

    Text {
      visible: !root.expanded
      textFormat: Text.PlainText
      text: Model.folderGroupText(root.folders)
      color: root.busy ? Color.accent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      text: "󰅀"
      rotation: root.expanded ? 180 : 0
      color: mouse.containsMouse ? Color.accent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      Behavior on rotation { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
    }
  }

  PanelToolTip {
    visible: mouse.containsMouse && !root.expanded && root.names !== ""
    text: root.names
    fontFamily: root.fontFamily
  }
}
