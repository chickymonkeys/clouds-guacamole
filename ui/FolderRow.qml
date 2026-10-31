import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
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
  property bool filtering: false
  // Off while the panel is closed: spinners in a hidden window still tick
  property bool animate: true

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property string state_: folder.state || ""
  readonly property bool syncing: state_ === "syncing"
  readonly property bool needsAttention: state_ === "attention" || (folder.attention || "") !== ""
  readonly property bool showActions: hover.hovered || confirmingRemove || editing || filtering
  // The name and everything under it start here
  readonly property real glyphWidth: Style.space(16)
  readonly property real textIndent: glyphWidth + Style.space(8)
  readonly property color stateColor: {
    if (needsAttention || state_ === "error") return Commons.Color.urgent
    if (syncing || state_ === "changes" || state_ === "pending") return Commons.Color.accent
    return dim
  }
  readonly property real progress: {
    var p = folder.progress || {}
    if (p.totalBytes > 0) return Math.min(1, p.bytes / p.totalBytes)
    if (p.totalTransfers > 0) return Math.min(1, p.transfers / p.totalTransfers)
    return -1
  }
  readonly property bool hasEdits: editing && labelInput.text.trim() !== String(folder.label || "")
  // Never sync: the global list, or the folder's own list instead (none: the global list)
  readonly property var ownExcludes: folder.excludes || []
  readonly property bool hasOwnList: ownExcludes.length > 0
  readonly property var globalExcludes: service ? (service.filters.excludes || []) : []
  property bool draftOwn: false
  // Saving an emptied own list goes back to the global one
  readonly property bool filterEdits: filtering && (draftOwn !== hasOwnList || (draftOwn && excludeEditor.edited))
  readonly property bool hasDetails: syncing || needsAttention || confirmingRemove || editing || filtering
                                     || (state_ === "error" && (folder.error || "") !== "")
                                     || (folder.conflictCount || 0) > 0
                                     || (folder.watchError || "") !== ""

  function resetEditor() {
    labelInput.text = String(folder.label || folder.defaultName || "")
  }

  function applyEdits() {
    root.service.setFolder(folder.id, labelInput.text.trim())
    root.editing = false
  }

  function openFilters() {
    draftOwn = hasOwnList
    // Against what the folder syncs with now, so the chips show what saving changes
    var current = hasOwnList ? ownExcludes : globalExcludes
    excludeEditor.reset(current, current)
    confirmingRemove = false
    editing = false
    filtering = true
  }

  function chooseOwn(own) {
    // A new own list starts as a copy of the global one
    if (own && !hasOwnList && !excludeEditor.edited) excludeEditor.reset(globalExcludes, globalExcludes)
    draftOwn = own
  }

  function applyFilters() {
    root.service.setExcludes(folder.id, draftOwn ? excludeEditor.result : [])
    filtering = false
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
          color: root.needsAttention ? Commons.Color.urgent : Qt.darker(root.foreground, 1.35)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          visible: root.syncing
          anchors.centerIn: parent
          text: "󰑐"
          color: Commons.Color.accent
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
        // Next to its mark when it has one, so the spacer after it takes the room
        Layout.maximumWidth: root.hasOwnList ? implicitWidth : Number.POSITIVE_INFINITY
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
          fontFamily: root.fontFamily
        }
      }

      // Its own never-synced list: a quiet mark, since it changes what this folder leaves out
      Text {
        visible: root.hasOwnList
        text: "󰈲"
        color: ownMarkHover.hovered ? Commons.Color.accent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption

        HoverHandler { id: ownMarkHover }

        PanelToolTip {
          visible: ownMarkHover.hovered
          text: "Never syncs its own list of " + root.ownExcludes.length
                + (root.ownExcludes.length === 1 ? " pattern" : " patterns") + " instead of the global one"
          fontFamily: root.fontFamily
        }
      }

      Item {
        visible: root.hasOwnList
        Layout.fillWidth: true
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
          hoverColor: Commons.Color.accent
          enabled: !root.syncing && !root.folder.paused
          onClicked: root.service.syncNow(root.folder.id)
        }

        PanelActionButton {
          iconText: "󰉋"
          tooltipText: "Open " + Model.tildify(root.folder.local, root.home)
          fontSize: Style.font.body
          foreground: root.foreground
          hoverColor: Commons.Color.accent
          onClicked: root.service.openPath(root.folder.local)
        }

        PanelActionButton {
          iconText: root.folder.paused ? "󰐊" : "󰏤"
          tooltipText: root.folder.paused ? "Resume syncing" : "Pause syncing"
          fontSize: Style.font.body
          foreground: root.foreground
          hoverColor: Commons.Color.accent
          onClicked: root.service.pauseFolder(root.folder.id, !root.folder.paused)
        }

        PanelActionButton {
          iconText: "󰈲"
          tooltipText: root.filtering ? "Close" : "Files this folder never syncs"
          fontSize: Style.font.body
          foreground: root.filtering ? Commons.Color.accent : root.foreground
          hoverColor: Commons.Color.accent
          onClicked: root.filtering ? (root.filtering = false) : root.openFilters()
        }

        PanelActionButton {
          iconText: "󰏫"
          tooltipText: root.editing ? "Close" : "Rename"
          fontSize: Style.font.body
          foreground: root.editing ? Commons.Color.accent : root.foreground
          hoverColor: Commons.Color.accent
          onClicked: {
            if (!root.editing) root.resetEditor()
            root.confirmingRemove = false
            root.filtering = false
            root.editing = !root.editing
          }
        }

        PanelActionButton {
          iconText: "󰆴"
          tooltipText: "Stop keeping this folder on this device"
          fontSize: Style.font.body
          foreground: root.confirmingRemove ? Commons.Color.urgent : root.dim
          hoverColor: Commons.Color.urgent
          onClicked: {
            root.editing = false
            root.filtering = false
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

      // Rename: a display name for the widget and the CLI, the folder's own name when empty
      BorderSurface {
        visible: root.editing
        Layout.fillWidth: true
        implicitHeight: editCol.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.04)
        borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

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

      // Never sync: every folder's list, or one just for this folder that replaces it
      BorderSurface {
        visible: root.filtering
        Layout.fillWidth: true
        implicitHeight: filterCol.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.04)
        borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

        ColumnLayout {
          id: filterCol
          anchors {
            fill: parent
            margins: Style.space(8)
          }
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            text: "Never sync in " + (root.folder.name || "this folder")
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            color: Qt.darker(root.foreground, 1.4)
          }

          ButtonGroup {
            Layout.fillWidth: true
            options: [
              { value: "global", label: "Same as all folders", tooltip: "The global list, from Settings → Filters" },
              { value: "own", label: "Just this folder", tooltip: "Its own list, used instead of the global one" }
            ]
            value: root.draftOwn ? "own" : "global"
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            focusable: false
            onChanged: function(v) { root.chooseOwn(v === "own") }
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(6)

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.draftOwn
                    ? "Replaces the global list here: later global changes won't apply to this folder."
                    : "Changes to the global list apply here too."
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption - Style.space(2)
              color: root.dim
              wrapMode: Text.WordWrap
            }

            Button {
              // Leaving with an unsaved choice would drop it
              visible: !root.draftOwn && !root.filterEdits
              text: "Edit global list"
              iconText: "󰁔"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption - Style.space(1)
              foreground: root.foreground
              bordered: false
              onClicked: {
                root.filtering = false
                root.service.showFilters()
              }
            }
          }

          // The global list, as it applies here
          ExcludeEditor {
            visible: !root.draftOwn
            Layout.fillWidth: true
            canEdit: false
            patterns: root.globalExcludes
            saved: root.globalExcludes
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          ExcludeEditor {
            id: excludeEditor
            visible: root.draftOwn
            Layout.fillWidth: true
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // What saving does, before it's done
          Text {
            visible: root.filterEdits
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: Model.filterChangeText(excludeEditor.added.length, excludeEditor.removed.length,
                                         root.draftOwn !== root.hasOwnList
                                           ? (root.draftOwn ? "Uses its own list" : "Back to the global list") : "",
                                         root.draftOwn && excludeEditor.result.length === 0)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(2)
            color: Commons.Color.accent
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
              enabled: root.filterEdits && !(root.draftOwn && excludeEditor.blocked)
              opacity: enabled ? 1.0 : 0.45
              onClicked: root.applyFilters()
            }

            Item { Layout.fillWidth: true }

            Button {
              text: root.filterEdits ? "Cancel" : "Close"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.foreground
              bordered: false
              onClicked: root.filtering = false
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
            color: Commons.Color.accent
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
        color: Qt.darker(Commons.Color.urgent, 1.15)
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
        color: Qt.alpha(Commons.Color.urgent, 0.08)
        borderSpec: Border.controlSpec("normal", Commons.Color.urgent, Commons.Color.urgent)

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
              visible: (parent.code === "safety" || parent.code === "empty") && !root.folder.resync
              text: "Sync anyway"
              tooltipText: "Apply the deletions to the other side too"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: Commons.Color.urgent
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
          color: Commons.Color.accent
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
          color: Commons.Color.urgent
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
            foreground: Commons.Color.urgent
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
