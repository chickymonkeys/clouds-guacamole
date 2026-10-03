import QtQuick
import qs.Commons

// Cloud glyph: filled when everything requested is healthy, a spinner in front while files move,
// and a small dot when something needs attention.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground
  property string fontFamily: Style.font.family
  property bool active: false
  property bool busy: false
  property bool attention: false

  width: iconSize * 1.2
  height: iconSize
  implicitWidth: iconSize * 1.2
  implicitHeight: iconSize

  Text {
    anchors.centerIn: parent
    text: root.active ? "󰅠" : "󰅟"
    color: root.color
    font.family: root.fontFamily
    font.pixelSize: root.iconSize
    opacity: root.busy ? (root.active ? 0.5 : 0.35) : (root.active ? 1.0 : 0.65)
    Behavior on opacity { NumberAnimation { duration: 150 } }
  }

  Text {
    visible: root.busy
    anchors.centerIn: parent
    text: "󰑐"
    color: Color.accent
    font.family: root.fontFamily
    font.pixelSize: root.iconSize * 0.8
    transformOrigin: Item.Center
    RotationAnimation on rotation {
      running: root.busy
      from: 0
      to: 360
      duration: 1100
      loops: Animation.Infinite
    }
  }

  Rectangle {
    visible: root.attention
    width: Math.max(4, root.iconSize * 0.32)
    height: width
    radius: width / 2
    color: Color.urgent
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.rightMargin: -width * 0.15
  }
}
