import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Never-synced patterns as chips. Edits a draft (`patterns`) against what is saved
// (`saved`): added chips are highlighted, removed ones stay struck through until saved and
// can be put back. Typing a pattern says in words what it matches, or what is wrong with it.
ColumnLayout {
  id: root

  property var patterns: []
  property var saved: []
  property bool canEdit: true
  property color foreground: Commons.Color.foreground
  property string fontFamily: Style.font.family
  property var suggestions: ["*.log", ".git/", "node_modules/", "__pycache__/", ".venv/", "build/", ".cache/", "*.iso"]

  readonly property color dim: Qt.darker(foreground, 1.6)
  // What the field holds, read as patterns: each one explained, or the first problem
  readonly property var typed: Model.parsePatterns(input.text, patterns)
  // What saving stores: the chips, and what is typed but not added yet (a Save right after
  // typing shouldn't lose it). Nothing saves while the field holds a broken pattern.
  readonly property var result: canEdit && !typed.error ? patterns.concat(typed.patterns) : patterns
  readonly property bool blocked: canEdit && typed.error !== ""
  readonly property var added: result.filter(function(p) { return saved.indexOf(p) === -1 })
  readonly property var removed: saved.filter(function(p) { return result.indexOf(p) === -1 })
  readonly property bool edited: added.length > 0 || removed.length > 0
  property bool helpOpen: false
  readonly property var examples: [
    ["*.log", "files ending in .log, anywhere in the folder"],
    ["*.{tmp,bak}", "files ending in .tmp or .bak"],
    ["build/", "every folder named build, with all inside"],
    ["/Exports/**", "everything in Exports, at the top only"],
    ["Drafts/*.pdf", "PDFs right inside any Drafts folder"],
    ["? and *", "one character, and any run of them within a name"]
  ]

  function reset(list, against) {
    patterns = (list || []).slice()
    root.saved = (against === undefined ? list || [] : against).slice()
    input.text = ""
  }

  function commit() {
    if (typed.error || typed.patterns.length === 0) return
    patterns = patterns.concat(typed.patterns)
    input.text = ""
  }

  function remove(p) { patterns = patterns.filter(function(x) { return x !== p }) }
  function restore(p) { patterns = patterns.concat([p]) }

  spacing: Style.space(6)

  component Chip: BorderSurface {
    id: chip
    property string pattern: ""
    // kept | added | removed | suggested | fixed
    property string kind: "kept"
    signal activated()

    readonly property bool interactive: kind !== "fixed"
    implicitWidth: chipRow.implicitWidth + Style.space(12)
    implicitHeight: chipRow.implicitHeight + Style.space(6)
    radius: Style.cornerRadius
    opacity: kind === "removed" ? 0.55 : 1
    color: kind === "added" ? Qt.alpha(Commons.Color.accent, 0.12)
         : kind === "suggested" ? "transparent"
         : chipMouse.containsMouse && kind !== "fixed" ? Qt.rgba(1, 1, 1, 0.07) : Qt.rgba(1, 1, 1, 0.04)
    borderSpec: Border.controlSpec(kind === "added" ? "selected" : (chipMouse.containsMouse && interactive ? "hover-cursor" : "normal"),
                                   kind === "suggested" ? root.dim : root.foreground, Commons.Color.accent)

    Row {
      id: chipRow
      anchors.centerIn: parent
      spacing: Style.space(5)

      Text {
        visible: chip.kind === "suggested"
        anchors.verticalCenter: parent.verticalCenter
        text: "󰐕"
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(2)
        color: chipMouse.containsMouse ? Commons.Color.accent : root.dim
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: chip.pattern
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        font.strikeout: chip.kind === "removed"
        color: chip.kind === "added" ? Commons.Color.accent
             : chip.kind === "suggested" ? (chipMouse.containsMouse ? root.foreground : root.dim)
             : root.foreground
      }

      Text {
        visible: chip.kind === "kept" || chip.kind === "added" || chip.kind === "removed"
        anchors.verticalCenter: parent.verticalCenter
        text: chip.kind === "removed" ? "󰕌" : "󰅖"
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(1)
        color: !chipMouse.containsMouse ? root.dim : (chip.kind === "removed" ? Commons.Color.accent : Commons.Color.urgent)
      }
    }

    MouseArea {
      id: chipMouse
      anchors.fill: parent
      hoverEnabled: true
      enabled: chip.interactive
      cursorShape: Qt.PointingHandCursor
      onClicked: chip.activated()
    }

    // Hover says what the pattern matches, and what a click does
    HoverHandler { id: chipHover }

    PanelToolTip {
      visible: chipHover.hovered
      fontFamily: root.fontFamily
      text: Model.explainPattern(chip.pattern)
            + (chip.kind === "removed" ? "\nRemoved: click to put it back"
             : chip.kind === "added" ? "\nNew: click to drop it"
             : chip.kind === "suggested" ? "\nClick to add"
             : chip.kind === "kept" ? "\nClick to remove" : "")
    }
  }

  // The list: what is (or will be) excluded, with what is about to stop being excluded
  Flow {
    visible: root.patterns.length + root.removed.length > 0
    Layout.fillWidth: true
    spacing: Style.space(5)

    Repeater {
      model: root.patterns
      Chip {
        required property string modelData
        pattern: modelData
        kind: !root.canEdit ? "fixed" : (root.saved.indexOf(modelData) === -1 ? "added" : "kept")
        onActivated: root.remove(modelData)
      }
    }

    Repeater {
      model: root.canEdit ? root.removed : []
      Chip {
        required property string modelData
        pattern: modelData
        kind: "removed"
        onActivated: root.restore(modelData)
      }
    }
  }

  Text {
    visible: root.patterns.length + root.removed.length === 0
    Layout.fillWidth: true
    textFormat: Text.PlainText
    text: root.canEdit ? "Nothing excluded yet: every file syncs. Add a pattern below." : "Nothing excluded: every file syncs."
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption - Style.space(1)
    color: root.dim
    wrapMode: Text.WordWrap
  }

  // Add one or several (comma-separated), with what each matches before it's added
  ColumnLayout {
    visible: root.canEdit
    Layout.fillWidth: true
    Layout.topMargin: Style.space(2)
    spacing: Style.space(4)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(6)

      TextField {
        id: input
        Layout.fillWidth: true
        placeholderText: "Add a pattern, e.g. *.log or build/"
        onAccepted: root.commit()
      }

      Button {
        iconText: "󰐕"
        text: "Add"
        tooltipText: "Several at once: separate them with commas"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        enabled: !root.typed.error && root.typed.patterns.length > 0
        opacity: enabled ? 1.0 : 0.45
        onClicked: root.commit()
      }
    }

    Text {
      visible: text !== ""
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.typed.error || root.typed.explained.join("\n")
      color: root.typed.error ? Commons.Color.urgent : Qt.darker(root.foreground, 1.25)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(2)
      wrapMode: Text.WordWrap
    }

    // Suggestions, then the syntax on demand
    Flow {
      readonly property var unused: root.suggestions.filter(function(p) { return root.patterns.indexOf(p) === -1 }).slice(0, 6)
      visible: unused.length > 0 && input.text === ""
      Layout.fillWidth: true
      spacing: Style.space(5)

      Text {
        height: Style.space(22)
        verticalAlignment: Text.AlignVCenter
        text: "Common"
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - Style.space(2)
        color: root.dim
      }

      Repeater {
        model: parent.unused
        Chip {
          required property string modelData
          pattern: modelData
          kind: "suggested"
          onActivated: root.restore(modelData)
        }
      }
    }

    Text {
      Layout.topMargin: Style.space(2)
      text: (root.helpOpen ? "󰅀" : "󰅂") + " How patterns work"
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(2)
      color: helpMouse.containsMouse ? Commons.Color.accent : root.dim

      MouseArea {
        id: helpMouse
        anchors.fill: parent
        anchors.margins: -Style.space(3)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.helpOpen = !root.helpOpen
      }
    }

    GridLayout {
      visible: root.helpOpen
      Layout.fillWidth: true
      Layout.leftMargin: Style.space(12)
      columns: 2
      columnSpacing: Style.space(12)
      rowSpacing: Style.space(3)

      Repeater {
        model: root.examples

        delegate: Text {
          required property var modelData
          required property int index
          Layout.row: index
          Layout.column: 0
          textFormat: Text.PlainText
          text: modelData[0]
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.foreground
        }
      }

      Repeater {
        model: root.examples

        delegate: Text {
          required property var modelData
          required property int index
          Layout.row: index
          Layout.column: 1
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: modelData[1]
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.dim
          wrapMode: Text.WordWrap
        }
      }
    }
  }
}
