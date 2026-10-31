import QtQuick
import Quickshell
import qs.Commons as Commons
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
      equal(Model.folderStateText({state: "waiting", errorKind: "network"}), "waiting for network")
      equal(Model.folderStateText({state: "waiting", errorKind: "network", localChanges: true}), "no network, changes waiting")
      equal(Model.folderStateText({state: "waiting", errorKind: "lock"}), "waiting for a previous sync")
      equal(Model.folderStateText({state: "offline"}), "offline")
      equal(Model.folderStateText({state: "offline", localChanges: true}), "offline, changes waiting")
      equal(Model.folderStateText({state: "offline", initialized: false}), "offline, not synced yet")
      equal(Model.folderGroup([{state: "waiting"}]).text, "waiting for network")
      equal(Model.folderGroup([{state: "offline"}, {state: "offline"}]).text, "offline")
      equal(Model.folderGroup([{state: "offline", localChanges: true}, {state: "paused"}]).text, "offline, 1 with changes")
      equal(Model.folderGroup([{state: "offline"}, {state: "syncing"}]).text, "1 syncing")
      equal(Model.folderGroup([{state: "offline", attention: "Check"}, {state: "offline"}]).text, "1 needs you")
      truth(!Model.folderStaysListed({state: "offline"}), "offline folders stay collapsed")
      truth(!Model.folderStaysListed({state: "waiting"}), "waiting needs no look")
      truth(Model.folderStaysListed({state: "offline", attention: "Check"}), "attention outranks offline")
      equal(Model.mountStateText({mountState: "offline", stream: true}), "offline")
      equal(Model.mountStateText({mountState: "off", stream: false}), "not streaming")
      equal(Model.workOfflineText({localChanges: 0}), "Working offline")
      equal(Model.workOfflineText({localChanges: 2}), "Working offline · 2 with changes")
      equal(Model.workOfflineText({syncing: 1, localChanges: 2}), "Finishing up, then offline…")
      equal(Model.workOfflineText({mounted: 1}), "Finishing up, then offline…")
      setFolder("attention", "safety")
    },
    function() {
      checkRow(["Resync", "Sync anyway", "Dismiss"])
      truth(Qt.colorEqual(row().stateColor, Commons.Color.urgent), "what needs you is in the theme's urgent color")
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
      setFolder("offline", "")
    },
    function() {
      equal(action("Sync now").enabled, false)
      truth(action("Pause syncing").enabled, "a folder's own pause stays available")
      setFolder("synced", "")
    },
    function() {
      truth(action("Sync now").enabled, "syncing again")
      panelState(false, 2)
    },
    function() {
      var toggle = cloudSwitch()
      truth(toggle.visible && toggle.checked, "on while anything is connected")
      equal(find(panel, function(i) { return i.visible && i.text === "Work offline" }), null)
      equal(panel.headline, "Up to date")
      equal(footerSync().enabled, true)
      equal(service.statusState, "online")
      truth(!driveSwitch().busy, "a drive's own switch works while online")
      truth(!heroIcon().offline && heroGlyph() === null, "no crossed-out cloud while online")
      panelState(false, 2, {syncing: 1})
    },
    function() {
      truth(service.busy, "files moving")
      truth(heroGlyph() === null, "files moving online keep the ordinary cloud")
      panelState(false, 2)
    },
    function() {
      var toggle = cloudSwitch()
      toggle.toggled()
      equal(root.calls[root.calls.length - 1], "offline:true")
      panelState(true, 2, {localChanges: 2, streaming: 1}, {stream: true, mountState: "offline"})
    },
    function() {
      var toggle = cloudSwitch()
      truth(!toggle.checked, "working offline is the switch's off position")
      truth(heroGlyph() !== null, "the header shows the crossed-out cloud")
      equal(panel.headline, "Working offline · 2 with changes")
      equal(footerSync().enabled, false)
      equal(service.statusState, "offline")
      truth(service.statusText.indexOf("working offline") !== -1, service.statusText)
      truth(service.statusText.indexOf("streaming") === -1, "not 0/1 streaming: " + service.statusText)
      equal(driveRow().subtitle, "offline")
      truth(!driveSwitch().checked, "the drive's switch says what is true now")
      truth(driveSwitch().busy, "and takes no clicks")
      toggle.toggled()
      equal(root.calls[root.calls.length - 1], "offline:false")
      panelState(true, 2, {syncing: 1})
    },
    function() {
      equal(panel.headline, "Finishing up, then offline…")
      truth(service.busy && heroGlyph() !== null, "crossed out while something still finishes")
      equal(heroGlyph().opacity, 1, "and at full strength, not faded under the spinner")
      panelState(true, 2, {mounted: 1, streaming: 1}, {
        stream: true, mountState: "mounted", mounted: true, retrying: true, errorKind: "uploads_pending",
        error: "2 file(s) are still uploading from Cloud. Wait, or unmount anyway."
      })
    },
    function() {
      equal(panel.headline, "Finishing up, then offline…")
      truth(driveRow().heldUp, "a drive that won't let go says so")
      truth(find(driveRow(), function(i) {
        return i.visible && i.text === "2 file(s) are still uploading from Cloud. Wait, or unmount anyway."
      }) !== null, "says why, without promising a retry")
      var unmount = find(driveRow(), function(i) { return i.visible && i.text === "Unmount anyway" })
      truth(unmount !== null, "offers the way out")
      unmount.clicked()
      equal(root.calls[root.calls.length - 1], "unmount:Cloud")
      panelState(false, 2, {mounted: 1, streaming: 1}, {
        stream: true, mountState: "mounted", mounted: true,
        error: "Not moved to /elsewhere yet: Cloud is in use"
      })
    },
    function() {
      truth(!driveRow().heldUp, "only on the way offline")
      equal(find(driveRow(), function(i) { return i.visible && i.text === "Unmount anyway" }), null)
      panelState(true, 2, {attention: 1})
    },
    function() {
      equal(panel.headline, "1 folder needs you")
      equal(service.statusState, "error")
      panelState(false, 2, {waitingNetwork: 1})
    },
    function() {
      equal(panel.headline, "Waiting for the network")
      equal(service.statusState, "offline")
      truth(service.statusText.indexOf("waiting for network") !== -1, service.statusText)
      panelState(false, 0)
    },
    function() {
      var toggle = cloudSwitch()
      truth(!toggle.checked, "off with nothing connected, without working offline")
      toggle.toggled()
      equal(root.calls[root.calls.length - 1], "streamAll:true")
      console.log("RECOVERY_WIDGET_PASS: recovery controls, collapsed visibility, state labels, plain folder titles and the Cloud Drives switch")
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
  function action(tooltip) {
    return find(row(), function(item) { return item.tooltipText === tooltip })
  }
  function cloudSwitch() {
    var found = find(panel, function(item) { return item.objectName === "cloudSwitch" })
    truth(found !== null, "production switch loaded")
    return found
  }
  function heroIcon() {
    var found = find(panel, function(item) {
      return typeof item.attention === "boolean" && typeof item.offline === "boolean"
    })
    truth(found !== null, "production CloudIcon loaded")
    return found
  }
  function heroGlyph() {
    return find(heroIcon(), function(item) { return item.text === "󰅤" })
  }
  function driveRow() {
    var found = find(panel, function(item) { return typeof item.heldUp === "boolean" })
    truth(found !== null, "production DriveRow loaded")
    return found
  }
  function driveSwitch() {
    return find(driveRow(), function(item) { return typeof item.busy === "boolean" && typeof item.toggled === "function" })
  }
  function footerSync() {
    return find(panel, function(item) { return item.tooltipText === "Sync local folders now (R)" })
  }
  function panelState(offline, folders, summary, drive) {
    service.connected = true
    service._applyState({
      engine: {state: "running", error: "", version: "test"},
      online: true,
      settings: {work_offline: offline},
      drives: [Object.assign({name: "Cloud", label: "Cloud", folders: [], stream: false, mountState: "off"}, drive || {})],
      summary: Object.assign({drives: 1, streaming: 0, mounted: 0, folders: folders, syncing: 0,
                              waitingNetwork: 0, localChanges: 0, attention: 0, uploads: 0}, summary || {})
    })
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

  // The real service, fed states by hand: only the command the switch sends is replaced
  G.Service {
    id: service
    function setWorkOffline(on) { root.calls.push("offline:" + on) }
    function streamAll(on) { root.calls.push("streamAll:" + on) }
    function unmountAnyway(remote) { root.calls.push("unmount:" + remote) }
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
    G.PanelContent {
      id: panel
      y: 300
      width: 450
      height: 280
      service: service
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
