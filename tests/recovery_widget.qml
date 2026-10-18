import QtQuick
import Quickshell
import "gui" as G
import "gui/Model.js" as Model

// Runs the production folder list and its existing recovery buttons off-screen.
ShellRoot {
  id: root
  property int step: 0
  property var calls: []
  property var checks: [
    function() {
      equal(Model.folderStateText({state: "syncing", resync: true, initialized: true}), "resyncing…")
      equal(Model.folderStateText({state: "syncing", resync: true, initialized: false}), "first sync…")
      equal(Model.folderStateText({state: "syncing", resync: false}), "syncing…")
      equal(Model.folderStateText({state: "syncing", resync: true, initialized: true,
                                  progress: {transfers: 1, totalTransfers: 3}}), "resyncing 1/3")
      equal(Model.folderStateText({state: "pending", resync: true, initialized: true}), "waiting to resync")
      setFolder("attention", "safety")
    },
    function() {
      checkRow(["Resync", "Sync anyway", "Dismiss"])
      click("Resync", "resync")
      click("Sync anyway", "force")
      click("Dismiss", "dismiss")
      setFolder("attention", "empty")
    },
    function() {
      checkRow(["Resync", "Sync anyway", "Dismiss"])
      setFolder("attention", "empty", 0, true)
    },
    function() {
      checkRow(["Resync", "Dismiss"])
      equal(button("Sync anyway"), null)
      setFolder("attention", "resync_failed")
    },
    function() {
      checkRow(["Resync", "Dismiss"])
      equal(button("Sync anyway"), null)
      click("Resync", "resync")
      setFolder("paused", "safety")
    },
    function() {
      checkRow(["Resync", "Sync anyway", "Dismiss"])
      equal(list.group.text, "1 needs you")
      setFolder("attention", "auth")
    },
    function() {
      checkRow(["Reconnect account", "Dismiss"])
      button("Reconnect account").clicked()
      equal(root.calls[root.calls.length - 1], "reconnect:Cloud")
      setFolder("synced", "", 2)
    },
    function() {
      truth(row().visible, "conflicts stay listed with the list closed")
      equal(list.group.text, "1 needs you")
      truth(Model.folderStaysListed(list.folders[0]), "conflict model visibility")
      truth(button("Clear") !== null, "conflicts offer Clear")
      button("Clear").clicked()
      equal(root.calls[root.calls.length - 1], "clear:a")
      setFolder("syncing", "", 2)
    },
    function() {
      truth(list.group.busy, "existing spinner survives conflicts during syncing")
      setFolder("synced", "")
    },
    function() {
      truth(!row().visible, "ordinary synced folders remain collapsed")
      equal(list.group.text, "synced")
      list.open = true
      list.folders = [Object.assign({}, list.folders[0], {name: "<b>Untrusted folder</b>"})]
    },
    function() {
      row().openFilters()
    },
    function() {
      var caption = find(row(), function(item) { return item.text === "Never sync in <b>Untrusted folder</b>" })
      truth(caption !== null, "filter editor title loaded")
      equal(caption.textFormat, Text.PlainText)
      console.log("RECOVERY_WIDGET_PASS: recovery controls, collapsed visibility, state labels and plain folder titles")
      Qt.quit()
    }
  ]

  function truth(value, message) {
    if (!value) throw new Error(message)
  }
  function equal(actual, expected) {
    if (actual !== expected) throw new Error("expected " + expected + ", got " + actual)
  }
  function find(item, predicate) {
    if (predicate(item)) return item
    var children = item.children || []
    for (var i = 0; i < children.length; i++) {
      var found = find(children[i], predicate)
      if (found) return found
    }
    return null
  }
  function row() {
    var result = find(list, function(item) { return typeof item.needsAttention === "boolean" })
    truth(result !== null, "production FolderRow loaded")
    return result
  }
  function button(text) {
    return find(row(), function(item) {
      return item.visible && item.text === text && typeof item.clicked === "function"
    })
  }
  function checkRow(buttons) {
    truth(row().visible && row().needsAttention, "attention remains visible in a collapsed list")
    for (var i = 0; i < buttons.length; i++) truth(button(buttons[i]) !== null, "missing " + buttons[i])
  }
  function click(text, action) {
    button(text).clicked()
    equal(root.calls[root.calls.length - 1], "resolve:a:" + action)
  }
  function setFolder(state, code, count, resync) {
    list.folders = [{id: "a", remote: "Cloud", name: "Notes", path: "Notes", local: "/tmp/notes",
                     state: state, attentionCode: code, attention: code ? "Check both copies" : "",
                     conflictCount: count || 0, conflicts: ["one.conflict1.md"], initialized: true,
                     resync: resync || false}]
  }

  QtObject {
    id: stubService
    property var filters: ({excludes: []})
    function resolveFolder(id, action) { root.calls.push("resolve:" + id + ":" + action) }
    function clearConflicts(id) { root.calls.push("clear:" + id) }
    function reconnect(remote) { root.calls.push("reconnect:" + remote) }
  }

  FloatingWindow {
    implicitWidth: 500
    implicitHeight: 600
    G.FolderList {
      id: list
      width: 450
      service: stubService
      open: false
      animate: false
    }
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: {
      try {
        root.checks[root.step++]()
      } catch (error) {
        console.error("RECOVERY_WIDGET_FAIL step " + root.step + ": " + error)
        Qt.quit()
      }
    }
  }
}
