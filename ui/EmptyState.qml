import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

Item {
  id: root

  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  signal addProvider(string providerId)

  implicitWidth: parent ? parent.width : Style.space(360)
  implicitHeight: col.implicitHeight + Style.space(20)

  ColumnLayout {
    id: col
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
      margins: Style.space(12)
    }
    spacing: Style.space(12)

    Text {
      Layout.alignment: Qt.AlignHCenter
      text: "No cloud drives yet"
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      font.bold: true
      color: root.foreground
    }

    Text {
      Layout.fillWidth: true
      text: "Stream a whole drive without using disk space, and keep the folders you work in on this device. Remotes you already set up with rclone appear here automatically."
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      color: Qt.darker(root.foreground, 1.6)
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
    }

    PanelSeparator {
      Layout.fillWidth: true
      foreground: root.foreground
    }

    PanelSectionHeader {
      text: "Quick connect"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Flow {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Repeater {
        model: [
          { id: "drive", glyph: "󰊭", label: "Google Drive" },
          { id: "onedrive", glyph: "󰏊", label: "OneDrive" },
          { id: "dropbox", glyph: "\uf16b", label: "Dropbox" },
          { id: "nextcloud", glyph: "󰒋", label: "Nextcloud" },
          { id: "", glyph: "󰅟", label: "Other…" }
        ]

        Button {
          required property var modelData
          iconText: modelData.glyph
          text: modelData.label
          fontFamily: root.fontFamily
          foreground: root.foreground
          bordered: true
          onClicked: root.addProvider(modelData.id)
        }
      }
    }
  }
}
