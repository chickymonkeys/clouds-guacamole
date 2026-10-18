import QtQuick
import Quickshell
import "gui" as G

ShellRoot {
  id: root
  property int phase: 0
  property int ticks: 0
  readonly property bool unsafe: Quickshell.env("GUAC_TEST_UNSAFE") === "1"

  function check(value, message) {
    if (!value) throw new Error(message)
  }
  function pass() {
    console.log("IPC_WIDGET_PASS: " + (unsafe ? "unsafe endpoint rejected" : "requests and reconnect"))
    Qt.quit()
  }
  function secretRequest(done) {
    service.request("set_client_id", {client_secret: "widget-fake-secret"},
      function(ok, data, error) {
        try {
          check(ok, error)
          check(data.echo.client_secret === "widget-fake-secret", "request/reply mismatch")
          done()
        } catch (e) {
          console.error("IPC_WIDGET_FAIL: " + e)
          Qt.quit()
        }
      })
  }

  G.Service {
    id: service
    pluginDir: Quickshell.env("GUAC_TEST_REPO")
  }

  Timer {
    interval: 50
    running: true
    repeat: true
    onTriggered: {
      try {
        root.ticks++
        check(root.ticks < 200, "connection timed out: " + service.startError)
        if (root.unsafe) {
          check(!service.connected && !service.everConnected, "unsafe server was trusted")
          if (root.ticks === 10) {
            service.request("set_client_id", {client_secret: "must-not-leak"},
              function(ok) { check(!ok, "unsafe request succeeded") })
          }
          if (root.ticks > 20 && service.startError.indexOf("runtime directory") !== -1) root.pass()
        } else if (root.phase === 0 && service.ready) {
          root.phase = 1
          root.secretRequest(function() {
            service.request("drop", {}, function(ok, data, error) {
              check(!ok && error.indexOf("Lost connection") !== -1, "in-flight request did not fail")
              root.phase = 2
            })
          })
        } else if (root.phase === 2 && service.ready && service.daemonVersion === "ipc-test-2") {
          root.phase = 3
          root.secretRequest(function() {
            check(service.pendingRequests === 0, "pending requests did not clear")
            root.pass()
          })
        }
      } catch (e) {
        console.error("IPC_WIDGET_FAIL: " + e)
        Qt.quit()
      }
    }
  }
}
