import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "gui" as G

// Drives the real panel through the README scenes and saves each one as a PNG.
// Run by render.py, which serves the demo state; the card mirrors Omarchy's KeyboardPanel.
ShellRoot {
  id: shell
  readonly property string out: Quickshell.env("SHOTS")
  property var queue: []
  property int qi: 0
  property int fails: 0

  FloatingWindow {
    implicitWidth: 520
    implicitHeight: 1500
    color: "transparent"

    G.Service {
      id: svc
      pluginDir: ""
    }

    BorderSurface {
      id: card
      property real cap: Style.space(620)
      width: Style.space(430)
      height: Math.min(cap, content.preferredHeight + contentTopInset + contentBottomInset)
      color: Color.popups.background
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.popupPadding
      radius: Style.cornerRadius

      G.PanelContent {
        id: content
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        service: svc
        statusColor: svc.statusState === "error" ? Color.urgent : (svc.statusState === "online" ? "#6fa96f" : "#c9a24a")
      }
    }

    // The bar icon in its four states
    BorderSurface {
      id: barStates
      y: 1300
      width: barRow.implicitWidth + padding * 2
      height: barRow.implicitHeight + padding * 2
      color: Color.bar.background
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.popupPadding
      radius: Style.cornerRadius

      Row {
        id: barRow
        x: barStates.padding
        y: barStates.padding
        spacing: Style.space(22)

        Repeater {
          model: [
            { label: "All good", active: true, busy: false, attention: false, color: "#6fa96f" },
            { label: "Files moving", active: true, busy: true, attention: false, color: "#6fa96f" },
            { label: "Offline or not streaming", active: false, busy: false, attention: false, color: "#c9a24a" },
            { label: "Needs you", active: false, busy: false, attention: true, color: Color.urgent }
          ]

          Row {
            required property var modelData
            spacing: Style.space(8)

            G.CloudIcon {
              anchors.verticalCenter: parent.verticalCenter
              iconSize: Style.bar.iconFont
              color: modelData.color
              active: modelData.active
              busy: modelData.busy
              attention: modelData.attention
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.label
              color: Qt.darker(Color.bar.text, 1.3)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- helpers
  function all(rootItem) {
    var out = []
    function rec(it) {
      if (!it) return
      out.push(it)
      for (var i = 0; i < it.children.length; i++) rec(it.children[i])
    }
    rec(rootItem || content)
    return out
  }
  function shown(rootItem) { return all(rootItem).filter(function(i) { return i.visible === true }) }
  function byProp(prop) {
    var hit = function(i) { return i[prop] !== undefined }
    return shown().filter(hit)[0] || all().filter(hit)[0]
  }
  function click(label, within) {
    var b = shown(within).filter(function(i) {
      return typeof i.clicked === "function" && i.iconText !== undefined && (i.text === label || i.tooltipText === label)
    })[0]
    if (!b) throw new Error("no visible button '" + label + "'")
    b.clicked()
    return true
  }
  function driveCard(name) {
    return shown().filter(function(i) { return i.keepFolderRequested !== undefined && i.drive && i.drive.name === name })[0]
  }
  function scene(name) { svc.request("demo_scene", { name: name }) }

  function step(desc, fn, delay) { queue.push({ desc: desc, fn: fn, delay: delay === undefined ? 700 : delay }) }
  function shot(name, item) {
    step("shot " + name, function() {
      (item || card).grabToImage(function(r) {
        r.saveToFile(shell.out + "/" + name + ".png")
        console.log("SHOT saved " + name)
      })
    }, 600)
  }

  function next() {
    if (qi >= queue.length) {
      console.log((fails ? "SHOT FAIL " + fails + " step(s) failed" : "SHOT done"))
      Qt.quit()
      return
    }
    var s = queue[qi++]
    try {
      if (s.fn() === false) throw new Error("check returned false")
    } catch (e) {
      fails++
      console.log("SHOT FAIL " + s.desc + ": " + e.message)
    }
    runner.interval = s.delay
    runner.restart()
  }

  Timer {
    id: runner
    interval: 3000
    running: true
    onTriggered: shell.next()
  }

  // ---------------------------------------------------------------- scenes
  Component.onCompleted: {
    step("connected", function() { return svc.ready && svc.drives.length === 3 })

    // Overview: streaming, a folder syncing, uploads, recent files
    step("expand recent files", function() {
      var rf = byProp("files")
      shown(rf).filter(function(i) { return i.cursorShape !== undefined && typeof i.clicked === "function" })[0].clicked(null)
      return true
    })
    step("full height", function() { card.cap = 4000; return true })
    step("Drive's folders open", function() { content.setFoldersOpen("Drive", true) })
    shot("panel")

    step("attention scene", function() { scene("attention"); content.setFoldersOpen("Drive", false) }, 1200)
    step("recent collapsed", function() {
      var rf = byProp("files")
      shown(rf).filter(function(i) { return i.cursorShape !== undefined && typeof i.clicked === "function" })[0].clicked(null)
      return true
    })
    shot("conflicts")

    // Many folders on one drive: closed down to the one that needs a look, then open
    step("many-folders scene", function() { scene("many") }, 1200)
    step("closed", function() { return driveCard("Drive").foldersOpen === false })
    shot("folder-list")
    step("open", function() { content.setFoldersOpen("Drive", true) })
    shot("folder-list-open")
    step("closed again", function() { content.setFoldersOpen("Drive", false) })
    step("only the conflicted folder still listed", function() {
      return shown(driveCard("Drive")).filter(function(i) { return i.folder !== undefined && i.folder.name }).map(function(i) {
        return i.folder.name
      }).join() === "Notes"
    })

    // Own client: the card's notice, then the guide
    step("shared-client scene", function() { scene("shared") }, 1200)
    step("Set up", function() { return click("Set up", driveCard("Drive")) }, 1500)
    step("guide: scopes step", function() {
      var g = byProp("stepCount")
      g.step = 3
      return g.current.title === "Add the Drive scopes"
    }, 1000)
    shot("client-guide")
    step("guide: fields", function() {
      var g = byProp("stepCount")
      g.step = g.stepCount - 1
      return click("Done, paste it")
    }, 1000)
    step("guide: paste", function() {
      var fields = shown(byProp("stepCount")).filter(function(i) { return i.placeholderText !== undefined && i.password !== undefined })
      // Obviously fake values in Google's format: never paste real credentials here
      fields[0].text = "000000000000-fakeclientforscreenshots.apps.googleusercontent.com"
      fields[1].text = "fake-secret-for-screenshots"
      return byProp("stepCount").valid
    }, 900)
    shot("client-paste")

    // Keep a folder on this device
    step("overview again", function() { content.showView("drives"); scene("overview") }, 1200)
    step("picker", function() { content.openBrowse("Drive") }, 1200)
    step("picker: Documents/Papers", function() { byProp("crumbs").navigate("Documents/Papers") }, 1500)
    shot("keep-folder")

    step("add drive", function() { content.showView("add") }, 1000)
    shot("add-drive")
    step("settings", function() { content.showView("settings") }, 1000)
    shot("settings")
    shot("bar-states", barStates)
  }
}
