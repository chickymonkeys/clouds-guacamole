import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// One folder kept on this device: a single line with its state, the actions on hover, and
// progress or what to do when it needs attention underneath.
CursorSurface {
  id: root

  property var folder: ({})
  property var service: null
  property string fontFamily: Style.font.family
  property string home: ""
  property bool confirmingRemove: false
  property bool editing: false
  // Off while the panel is closed: spinners in a hidden window still tick
  property bool animate: true

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property string state_: folder.state || ""
  readonly property bool syncing: state_ === "syncing"
  readonly property bool needsAttention: state_ === "attention"
  readonly property bool showActions: hover.hovered || confirmingRemove || editing
  // The name and everything under it start here
  readonly property real glyphWidth: Style.space(16)
  readonly property real textIndent: glyphWidth + Style.space(8)
  readonly property color stateColor: {
    if (needsAttention || state_ === "error") return Color.urgent
    if (syncing || state_ === "changes" || state_ === "pending") return Color.accent
    return dim
  }
  readonly property real progress: {
    var p = folder.progress || {}
    if (p.totalBytes > 0) return Math.min(1, p.bytes / p.totalBytes)
    if (p.totalTransfers > 0) return Math.min(1, p.transfers / p.totalTransfers)
    return -1
  }
  // Its own never-synced patterns, used instead of the global list; none: the global list
  readonly property var ownExcludes: folder.excludes || []
  readonly property var globalExcludes: service ? (service.filters.excludes || []) : []
  property bool draftOwn: false
  readonly property var draftExcludes: draftOwn ? excludeEditor.patterns : []
  readonly property bool labelEdited: labelInput.text.trim() !== String(folder.label || "")
  readonly property bool excludesEdited: JSON.stringify(draftExcludes) !== JSON.stringify(ownExcludes)
  readonly property bool hasEdits: editing && (labelEdited || excludesEdited)
  readonly property bool hasDetails: syncing || needsAttention || confirmingRemove || editing
                                     || (state_ === "error" && (folder.error || "") !== "")
                                     || (folder.conflictCount || 0) > 0
                                     || (folder.watchError || "") !== ""

  function resetEditor() {
    labelInput.text = String(folder.label || folder.defaultName || "")
    draftOwn = ownExcludes.length > 0
    excludeEditor.patterns = ownExcludes.slice()
  }

  function applyEdits() {
    if (labelEdited) root.service.setFolder(folder.id, labelInput.text.trim())
    // An emptied list goes back to the global one
    if (excludesEdited) root.service.setExcludes(folder.id, draftExcludes)
    root.editing = false
  }

  implicitHeight: col.implicitHeight + Style.space(6)
  hasCursor: hover.hovered

  HoverHandler { id: hover }

  ColumnLayout {
    id: col
    // From the top, so closing the list rolls the row up from below
    anchors {
      left: parent.left
      right: parent.right
      top: parent.top
      topMargin: Style.space(3)
      leftMargin: Style.space(8)
      rightMargin: Style.space(4)
    }
    spacing: 0

    // Fixed to the action buttons' height, so swapping the state for them never moves anything
    RowLayout {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(22)
      spacing: Style.space(8)

      Item {
        implicitWidth: root.glyphWidth
        implicitHeight: glyph.implicitHeight

        Text {
          id: glyph
          visible: !root.syncing
          anchors.centerIn: parent
          text: root.folder.paused ? "󰏤" : "󰉋"
          color: root.needsAttention ? Color.urgent : Qt.darker(root.foreground, 1.35)
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
            running: root.syncing && root.animate && root.visible
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
        text: root.folder.name || "/"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight

        HoverHandler { id: nameHover }

        PanelToolTip {
          visible: nameHover.hovered
          text: Model.tildify(root.folder.local, root.home) + "  ⇄  " + root.folder.remote + ":" + (root.folder.path || "/")
                + (root.ownExcludes.length > 0 ? "\nNever syncs its own " + root.ownExcludes.length
                   + (root.ownExcludes.length === 1 ? " pattern" : " patterns") : "")
          fontFamily: root.fontFamily
        }
      }

      Text {
        visible: !root.showActions
        textFormat: Text.PlainText
        text: Model.folderStateText(root.folder)
        color: root.stateColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Row {
        visible: root.showActions
        spacing: Style.space(2)

        PanelActionButton {
          iconText: "󰑐"
          tooltipText: "Sync now"
          fontSize: Style.font.body
          foreground: root.foreground
          hoverColor: Color.accent
          enabled: !root.syncing && !root.folder.paused
          onClicked: root.service.syncNow(root.folder.id)
        }

        PanelActionButton {
          iconText: "󰉋"
          tooltipText: "Open " + Model.tildify(root.folder.local, root.home)
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
          iconText: "󰏫"
          tooltipText: root.editing ? "Close" : "Rename, never-synced files"
          fontSize: Style.font.body
          foreground: root.editing ? Color.accent : root.foreground
          hoverColor: Color.accent
          onClicked: {
            if (!root.editing) root.resetEditor()
            root.confirmingRemove = false
            root.editing = !root.editing
          }
        }

        PanelActionButton {
          iconText: "󰆴"
          tooltipText: "Stop keeping this folder on this device"
          fontSize: Style.font.body
          foreground: root.confirmingRemove ? Color.urgent : root.dim
          hoverColor: Color.urgent
          onClicked: {
            root.editing = false
            root.confirmingRemove = !root.confirmingRemove
          }
        }
      }
    }

    ColumnLayout {
      visible: root.hasDetails
      Layout.fillWidth: true
      Layout.leftMargin: root.textIndent
      Layout.topMargin: Style.space(2)
      Layout.bottomMargin: Style.space(4)
      spacing: Style.space(6)

      // Rename: a display name for the widget and the CLI, the folder's own name when empty.
      // Never sync: the global patterns, or the folder's own list instead
      BorderSurface {
        visible: root.editing
        Layout.fillWidth: true
        implicitHeight: editCol.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.04)
        borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

        ColumnLayout {
          id: editCol
          anchors {
            fill: parent
            margins: Style.space(8)
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
            placeholderText: root.folder.defaultName || ""
            onAccepted: if (root.hasEdits) root.applyEdits()
          }

          RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: Style.space(4)
            spacing: Style.space(8)

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0

              Text {
                text: "Own never-synced files"
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                color: Qt.darker(root.foreground, 1.4)
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.draftOwn
                      ? "Replaces the global list for this folder (later global changes don't apply). Emptying it goes back to the global list."
                      : "Uses the global list (" + root.globalExcludes.length + " patterns, Settings → Filters)."
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption - Style.space(2)
                color: root.dim
                wrapMode: Text.WordWrap
              }
            }

            ToggleSwitch {
              checked: root.draftOwn
              foreground: root.foreground
              onToggled: {
                // Starts from the global list, the one the folder was using
                if (!root.draftOwn && excludeEditor.patterns.length === 0)
                  excludeEditor.patterns = root.globalExcludes.slice()
                root.draftOwn = !root.draftOwn
              }
            }
          }

          ExcludeEditor {
            id: excludeEditor
            visible: root.draftOwn
            Layout.fillWidth: true
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            visible: root.excludesEdited
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: "Saving makes the next sync a safe resync: nothing is deleted, the newer copy wins."
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(2)
            color: Color.accent
            wrapMode: Text.WordWrap
          }

          RowLayout {
            Layout.fillWidth: true
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
        }
      }

      // Progress while syncing
      ColumnLayout {
        visible: root.syncing
        Layout.fillWidth: true
        spacing: Style.space(4)

        Rectangle {
          id: track
          Layout.fillWidth: true
          implicitHeight: Style.space(3)
          radius: height / 2
          color: Qt.rgba(1, 1, 1, 0.08)
          // Sweeps back and forth while the total isn't known yet
          property real sweep: 0

          SequentialAnimation on sweep {
            running: root.syncing && root.progress < 0 && root.animate && root.visible
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
            return parts.join("  ·  ")
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
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
        maximumLineCount: 3
        elide: Text.ElideRight
      }

      // Needs a decision before syncing continues
      BorderSurface {
        visible: root.needsAttention
        Layout.fillWidth: true
        implicitHeight: attCol.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: Qt.alpha(Color.urgent, 0.08)
        borderSpec: Border.controlSpec("normal", Color.urgent, Color.urgent)

        ColumnLayout {
          id: attCol
          anchors {
            fill: parent
            margins: Style.space(8)
          }
          spacing: Style.space(8)

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: root.folder.attention || ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            lineHeight: 1.15
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

      // Conflicts: both versions were kept; the files are listed on hover
      RowLayout {
        visible: (root.folder.conflictCount || 0) > 0
        Layout.fillWidth: true
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: "󰀦 " + root.folder.conflictCount + (root.folder.conflictCount === 1 ? " conflict" : " conflicts")
                + ", both versions kept"
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight

          HoverHandler { id: conflictHover }

          PanelToolTip {
            visible: conflictHover.hovered
            text: (root.folder.conflicts || []).join("\n")
            fontFamily: root.fontFamily
          }
        }

        Button {
          text: "Clear"
          tooltipText: "Forget these conflicts; the files stay"
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
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
        spacing: Style.space(6)

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
}
