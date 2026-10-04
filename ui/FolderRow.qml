import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// One folder kept on this device: state, progress, and what to do when it needs attention.
CursorSurface {
  id: root

  property var folder: ({})
  property var service: null
  property string fontFamily: Style.font.family
  property string home: ""
  property bool confirmingRemove: false

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property string state_: folder.state || ""
  readonly property bool syncing: state_ === "syncing"
  readonly property bool needsAttention: state_ === "attention"
  readonly property color stateColor: {
    if (needsAttention || state_ === "error") return Color.urgent
    if (state_ === "synced") return Qt.darker(foreground, 1.3)
    if (syncing || state_ === "changes" || state_ === "pending") return Color.accent
    return dim
  }
  readonly property real progress: {
    var p = folder.progress || {}
    if (p.totalBytes > 0) return Math.min(1, p.bytes / p.totalBytes)
    if (p.totalTransfers > 0) return Math.min(1, p.transfers / p.totalTransfers)
    return -1
  }

  implicitHeight: col.implicitHeight + Style.space(10)
  hasCursor: hover.containsMouse

  MouseArea {
    id: hover
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.NoButton
  }

  ColumnLayout {
    id: col
    anchors {
      left: parent.left
      right: parent.right
      verticalCenter: parent.verticalCenter
      leftMargin: Style.space(6)
      rightMargin: Style.space(4)
    }
    spacing: Style.space(3)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(6)

      Item {
        implicitWidth: glyph.implicitWidth
        implicitHeight: glyph.implicitHeight

        Text {
          id: glyph
          visible: !root.syncing
          text: root.folder.paused ? "󰏤" : "󰉋"
          color: root.needsAttention ? Color.urgent : Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          visible: root.syncing
          anchors.centerIn: parent
          text: "󰑐"
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          RotationAnimation on rotation {
            running: root.syncing
            from: 0
            to: 360
            duration: 1100
            loops: Animation.Infinite
          }
        }
      }

      Text {
        textFormat: Text.PlainText
        text: root.folder.name || "/"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideRight
        Layout.maximumWidth: Style.space(130)
      }

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: Model.folderStateText(root.folder)
        color: root.stateColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      PanelActionButton {
        iconText: "󰑐"
        tooltipText: "Sync now"
        fontSize: Style.font.body
        foreground: root.foreground
        hoverColor: Color.accent
        enabled: !root.syncing && !root.folder.paused
        opacity: enabled ? 1 : 0.4
        onClicked: root.service.syncNow(root.folder.id)
      }

      PanelActionButton {
        iconText: "󰉋"
        tooltipText: "Open the local copy"
        fontSize: Style.font.body
        foreground: root.foreground
        hoverColor: Color.accent
        onClicked: root.service.openPath(root.folder.local)
      }

      PanelActionButton {
        iconText: root.folder.paused ? "󰐊" : "󰏤"
        tooltipText: root.folder.paused ? "Resume syncing" : "Pause syncing"
        fontSize: Style.font.body
        foreground: root.foreground
        hoverColor: Color.accent
        onClicked: root.service.pauseFolder(root.folder.id, !root.folder.paused)
      }

      PanelActionButton {
        iconText: "󰆴"
        tooltipText: "Stop keeping this folder on this device"
        fontSize: Style.font.body
        foreground: root.confirmingRemove ? Color.urgent : root.dim
        hoverColor: Color.urgent
        onClicked: root.confirmingRemove = !root.confirmingRemove
      }
    }

    Text {
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: Model.tildify(root.folder.local, root.home) + "  ⇄  " + root.folder.remote + ":" + (root.folder.path || "/")
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(1)
      elide: Text.ElideMiddle
    }

    // Progress while syncing
    ColumnLayout {
      visible: root.syncing
      Layout.fillWidth: true
      spacing: Style.space(2)

      Rectangle {
        id: track
        Layout.fillWidth: true
        implicitHeight: Style.space(3)
        radius: height / 2
        color: Qt.rgba(1, 1, 1, 0.08)
        // Sweeps back and forth while the total isn't known yet
        property real sweep: 0

        SequentialAnimation on sweep {
          running: root.syncing && root.progress < 0
          loops: Animation.Infinite
          NumberAnimation { from: 0; to: 1; duration: 900; easing.type: Easing.InOutQuad }
          NumberAnimation { from: 1; to: 0; duration: 900; easing.type: Easing.InOutQuad }
        }

        Rectangle {
          height: parent.height
          radius: height / 2
          color: Color.accent
          width: root.progress >= 0 ? parent.width * root.progress : parent.width * 0.3
          x: root.progress >= 0 ? 0 : track.sweep * (parent.width - width)
          Behavior on width { NumberAnimation { duration: 200 } }
        }
      }

      Text {
        visible: text !== ""
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: {
          var p = root.folder.progress || {}
          var parts = []
          if (p.current) parts.push(p.current)
          if (p.totalBytes > 0) parts.push(Model.formatBytes(p.bytes) + " of " + Model.formatBytes(p.totalBytes))
          if (p.speed > 0) parts.push(Model.formatSpeed(p.speed))
          return parts.join(" · ")
        }
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        elide: Text.ElideMiddle
      }
    }

    // Retrying after an error
    Text {
      visible: root.state_ === "error" && (root.folder.error || "") !== ""
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.folder.error || ""
      color: Qt.darker(Color.urgent, 1.15)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(1)
      wrapMode: Text.WordWrap
      maximumLineCount: 3
      elide: Text.ElideRight
    }

    // Needs a decision before syncing continues
    BorderSurface {
      visible: root.needsAttention
      Layout.fillWidth: true
      implicitHeight: attCol.implicitHeight + Style.space(12)
      radius: Style.cornerRadius
      color: Qt.alpha(Color.urgent, 0.08)
      borderSpec: Border.controlSpec("normal", Color.urgent, Color.urgent)

      ColumnLayout {
        id: attCol
        anchors {
          fill: parent
          margins: Style.space(6)
        }
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.folder.attention || ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Flow {
          Layout.fillWidth: true
          spacing: Style.space(6)
          readonly property string code: root.folder.attentionCode || ""

          Button {
            visible: ["safety", "empty", "resync_failed", "missing_local"].indexOf(parent.code) !== -1
            text: parent.code === "missing_local" ? "Download again" : "Resync"
            tooltipText: "Merge both sides again. Nothing is deleted."
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.resolveFolder(root.folder.id, "resync")
          }

          Button {
            visible: parent.code === "safety" || parent.code === "empty"
            text: "Sync anyway"
            tooltipText: "Apply the deletions to the other side too"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: Color.urgent
            bordered: true
            onClicked: root.service.resolveFolder(root.folder.id, "force")
          }

          Button {
            visible: parent.code === "auth"
            text: "Reconnect account"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.reconnect(root.folder.remote)
          }

          Button {
            text: "Dismiss"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.dim
            bordered: false
            onClicked: root.service.resolveFolder(root.folder.id, "dismiss")
          }
        }
      }
    }

    // Conflicts: both versions were kept
    RowLayout {
      visible: (root.folder.conflictCount || 0) > 0
      Layout.fillWidth: true
      spacing: Style.space(6)

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: "󰀦 " + root.folder.conflictCount + (root.folder.conflictCount === 1 ? " conflict" : " conflicts")
              + ", both versions kept: " + (root.folder.conflicts || []).join(", ")
        color: Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        elide: Text.ElideRight
      }

      Button {
        text: "Clear"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption - Style.space(1)
        foreground: root.dim
        bordered: false
        onClicked: root.service.clearConflicts(root.folder.id)
      }
    }

    Text {
      visible: (root.folder.watchError || "") !== ""
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.folder.watchError || ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(1)
      wrapMode: Text.WordWrap
    }

    // Remove confirmation
    ColumnLayout {
      visible: root.confirmingRemove
      Layout.fillWidth: true
      spacing: Style.space(4)

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: "Stop syncing " + (root.folder.name || "this folder") + "? The cloud copy is not touched."
        color: Color.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Flow {
        Layout.fillWidth: true
        spacing: Style.space(6)

        Button {
          text: "Stop, keep local files"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.foreground
          bordered: true
          onClicked: {
            root.confirmingRemove = false
            root.service.removeFolder(root.folder.id, false)
          }
        }

        Button {
          text: "Stop and trash local copy"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: Color.urgent
          bordered: true
          onClicked: {
            root.confirmingRemove = false
            root.service.removeFolder(root.folder.id, true)
          }
        }

        Button {
          text: "Cancel"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          foreground: root.dim
          bordered: false
          onClicked: root.confirmingRemove = false
        }
      }
    }
  }
}
