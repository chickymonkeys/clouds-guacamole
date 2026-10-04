import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// The last lines of guacd's and rclone's logs, newest first, without leaving the panel.
Item {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  property string source: "daemon"
  property bool problemsOnly: false
  property var lines: []
  property string path: ""
  property string error: ""
  property bool loading: false

  signal back()

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property var shown: {
    var out = []
    for (var i = lines.length - 1; i >= 0; i--) {
      if (!problemsOnly || /ERROR|WARNING|CRITICAL|NOTICE|fail/i.test(lines[i])) out.push(lines[i])
    }
    return out
  }

  implicitHeight: col.implicitHeight

  function load(src) {
    if (src) source = src
    if (!service) return
    loading = true
    service.fetchLog(source, 200, function(result, filePath, err) {
      root.loading = false
      root.lines = result || []
      root.path = filePath || ""
      root.error = result ? "" : (err || "Could not read the log")
    })
  }

  ColumnLayout {
    id: col
    width: parent.width
    spacing: Style.space(10)

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
        text: "Log"
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        color: root.foreground
      }

      ButtonGroup {
        options: [
          { value: "daemon", label: "Guacamole", tooltip: "What the background service did" },
          { value: "rclone", label: "rclone", tooltip: "Transfers, mounts and errors from rclone" }
        ]
        value: root.source
        focusable: false
        spacing: Style.space(4)
        background: "transparent"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        onChanged: function(v) { root.load(v) }
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: root.loading ? "Reading…" : (root.shown.length + (root.problemsOnly ? " problem lines" : " lines") + ", newest first")
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        color: root.dim
      }

      Text {
        text: "Problems only"
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        color: root.foreground
      }

      ToggleSwitch {
        checked: root.problemsOnly
        foreground: root.foreground
        onToggled: root.problemsOnly = !root.problemsOnly
      }
    }

    BorderSurface {
      Layout.fillWidth: true
      implicitHeight: logText.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.rgba(0, 0, 0, 0.2)
      borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

      TextEdit {
        id: logText
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        readOnly: true
        selectByMouse: true
        textFormat: TextEdit.PlainText
        wrapMode: TextEdit.WrapAnywhere
        text: root.error !== "" ? root.error
              : (root.shown.length > 0 ? root.shown.join("\n") : (root.loading ? "" : "Nothing logged yet."))
        font.family: "monospace"
        font.pixelSize: Style.font.caption - Style.space(2)
        color: root.error !== "" ? Color.urgent : root.foreground
        selectionColor: Qt.alpha(Color.accent, 0.4)
      }
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(6)

      Button {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        iconText: "󰑐"
        text: "Refresh"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        onClicked: root.load("")
      }

      Button {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        iconText: "󰆏"
        text: "Copy"
        tooltipText: "Copy the lines shown, oldest first"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        enabled: root.shown.length > 0
        opacity: enabled ? 1 : 0.45
        onClicked: root.service.copyText(root.shown.slice().reverse().join("\n"))
      }

      Button {
        Layout.fillWidth: true
        Layout.preferredWidth: 1
        iconText: "󰏌"
        text: "Open file"
        tooltipText: root.path
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        enabled: root.path !== ""
        opacity: enabled ? 1 : 0.45
        onClicked: root.service.openPath(root.path)
      }
    }
  }
}
