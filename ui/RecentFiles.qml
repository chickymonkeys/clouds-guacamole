import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Collapsible list of recently used cloud files: opened through a mount (served from the
// cache) or changed in a folder kept on this device. Nothing here touches the network.
ColumnLayout {
  id: root

  property var files: []
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property bool expanded: false
  property real maxListHeight: Style.space(170)

  signal fileSelected(string filePath)

  spacing: Style.space(4)
  visible: files && files.length > 0

  PanelSeparator {
    Layout.fillWidth: true
    foreground: root.foreground
  }

  Item {
    Layout.fillWidth: true
    implicitHeight: Style.space(30)

    MouseArea {
      id: headerMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.expanded = !root.expanded
    }

    RowLayout {
      anchors {
        fill: parent
        rightMargin: Style.space(8)
      }
      spacing: Style.space(8)

      PanelSectionHeader {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        topPadding: 0
        text: "Recent Files"
        foreground: headerMouse.containsMouse ? Qt.lighter(root.foreground, 1.4) : root.foreground
        fontFamily: root.fontFamily
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
          text: String(root.files ? root.files.length : 0)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: Qt.darker(root.foreground, 1.3)
        }
      }

      Text {
        Layout.alignment: Qt.AlignVCenter
        text: "󰅀"
        rotation: root.expanded ? 0 : -90
        color: headerMouse.containsMouse ? Color.accent : Qt.darker(root.foreground, 1.2)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        Behavior on rotation { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
      }
    }
  }

  ListView {
    Layout.fillWidth: true
    Layout.preferredHeight: root.expanded ? Math.min(contentHeight, root.maxListHeight) : 0
    visible: Layout.preferredHeight > 0
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    Behavior on Layout.preferredHeight { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

    model: root.files ? root.files.length : 0

    delegate: CursorSurface {
      id: fileRow
      required property int index
      readonly property var file: root.files[index] || ({})
      width: ListView.view.width
      implicitHeight: Style.space(34)
      hasCursor: rowMouse.containsMouse

      MouseArea {
        id: rowMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.fileSelected(fileRow.file.path)
      }

      RowLayout {
        anchors {
          fill: parent
          leftMargin: Style.space(8)
          rightMargin: Style.space(8)
        }
        spacing: Style.space(8)

        Text {
          text: Model.fileGlyph(fileRow.file.name)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          color: Color.accent
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: fileRow.file.name || ""
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            color: root.foreground
            elide: Text.ElideRight
          }

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: (fileRow.file.where === "local" ? "󰋊 " : "󰅟 ") + fileRow.file.remote + " · " + (fileRow.file.folder || "/")
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
            color: Qt.darker(root.foreground, 1.7)
            elide: Text.ElideMiddle
          }
        }

        Text {
          text: Model.ago(fileRow.file.modifiedTs)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: Qt.darker(root.foreground, 1.8)
        }
      }
    }
  }
}
