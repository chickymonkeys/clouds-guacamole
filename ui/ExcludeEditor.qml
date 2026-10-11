import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// A list of never-synced patterns as removable chips, with a field to add one. Edits a draft:
// the owner reads `patterns` and saves it.
ColumnLayout {
  id: root

  property var patterns: []
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string placeholder: "*.log, build/, /Exports/**"

  readonly property color dim: Qt.darker(foreground, 1.6)

  function add(text) {
    var p = String(text || "").trim().replace(/^- /, "").trim()
    if (p === "" || patterns.indexOf(p) !== -1) return false
    patterns = patterns.concat([p])
    return true
  }

  function remove(p) {
    patterns = patterns.filter(function(x) { return x !== p })
  }

  spacing: Style.space(6)

  Flow {
    visible: root.patterns.length > 0
    Layout.fillWidth: true
    spacing: Style.space(4)

    Repeater {
      model: root.patterns

      BorderSurface {
        id: chip
        required property string modelData
        implicitWidth: chipRow.implicitWidth + Style.space(10)
        implicitHeight: chipRow.implicitHeight + Style.space(4)
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.04)
        borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

        Row {
          id: chipRow
          anchors.centerIn: parent
          spacing: Style.space(4)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: chip.modelData
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
            color: root.foreground
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "󰅖"
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
            color: removeMouse.containsMouse ? Color.urgent : root.dim

            MouseArea {
              id: removeMouse
              anchors.fill: parent
              anchors.margins: -Style.space(3)
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.remove(chip.modelData)
            }
          }
        }
      }
    }
  }

  Text {
    visible: root.patterns.length === 0
    Layout.fillWidth: true
    textFormat: Text.PlainText
    text: "Nothing is excluded: every file syncs."
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption - Style.space(1)
    color: root.dim
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.space(6)

    TextField {
      id: addField
      Layout.fillWidth: true
      placeholderText: root.placeholder
      onAccepted: if (root.add(text)) text = ""
    }

    Button {
      iconText: "󰐕"
      text: "Add"
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      foreground: root.foreground
      bordered: true
      enabled: addField.text.trim() !== ""
      opacity: enabled ? 1.0 : 0.45
      onClicked: if (root.add(addField.text)) addField.text = ""
    }
  }
}
