import QtQuick
import qs.Commons
import qs.Commons as Commons

// Cloud glyph: filled when everything requested is healthy, crossed out while working offline,
// a spinner in front while files move, and a small dot when something needs attention.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Commons.Color.foreground
  property string fontFamily: Style.font.family
  property bool active: false
  // Chosen, not a fault: drawn at full strength, unlike the faded cloud of a missing network,
  // and from the moment it's chosen, even with something still finishing
  property bool offline: false
  property bool busy: false
  property bool attention: false
  // Off while nobody can see it (a closed panel), so the spinner doesn't keep ticking
  property bool animate: true

  width: iconSize * 1.2
  height: iconSize
  implicitWidth: iconSize * 1.2
  implicitHeight: iconSize

  Text {
    anchors.centerIn: parent
    text: root.offline ? "󰅤" : (root.active ? "󰅠" : "󰅟")
    color: root.color
    font.family: root.fontFamily
    font.pixelSize: root.iconSize
    opacity: root.offline ? 1.0 : (root.busy ? (root.active ? 0.5 : 0.35) : (root.active ? 1.0 : 0.65))
    Behavior on opacity { NumberAnimation { duration: 150 } }
  }

  Text {
    visible: root.busy
    anchors.centerIn: parent
    // In the corner while offline: over the cloud it would read as files moving
    anchors.horizontalCenterOffset: root.offline ? root.iconSize * 0.55 : 0
    anchors.verticalCenterOffset: root.offline ? root.iconSize * 0.3 : 0
    text: "󰑐"
    color: Commons.Color.accent
    font.family: root.fontFamily
    font.pixelSize: root.iconSize * (root.offline ? 0.5 : 0.8)
    transformOrigin: Item.Center
    RotationAnimation on rotation {
      running: root.busy && root.animate
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
    color: Commons.Color.urgent
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.rightMargin: -width * 0.15
  }
}
