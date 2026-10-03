import QtQuick
import Quickshell
import Quickshell.Io

// Connection to guacd. The daemon pushes its whole state as one JSON line whenever something
// changes, so the widget never polls and never starts a process to learn what is going on.
// Commands go over the same socket and are answered by id.
Item {
  id: root

  property var settings: ({})
  property string pluginDir: ""
  readonly property string cli: pluginDir + "/bin/guac"
  readonly property string home: String(Quickshell.env("HOME") || "")
  readonly property string socketPath: {
    var rt = String(Quickshell.env("XDG_RUNTIME_DIR") || "")
    var p = rt !== "" ? rt + "/guacamole/daemon.sock" : ""
    // Mirrors guac/paths.py: unix socket paths are limited to 108 bytes
    if (p === "" || p.length > 100) return "/tmp/guacamole-" + String(Quickshell.env("UID") || "") + "/daemon.sock"
    return p
  }

  // ---- daemon state (replaced wholesale on every push)
  property bool connected: false
  property bool everConnected: false
  property bool starting: false
  property string startError: ""
  property var engine: ({ state: "starting", error: "", version: "" })
  property bool online: true
  property string mountRoot: home + "/Cloud/Stream"
  property string localRoot: home + "/Cloud/Sync"
  // Shared parent of the two roots (~/Cloud): what "open the cloud folder" opens
  property string cloudRoot: home + "/Cloud"
  property var appSettings: ({})
  property var drives: []
  property var summary: ({ drives: 0, streaming: 0, mounted: 0, folders: 0, syncing: 0, attention: 0, uploads: 0 })
  property var transfers: ({ count: 0, speed: 0, names: [] })
  property var recent: []
  property var auth: ({ busy: false, waiting: false, url: "", error: "", success: "", remote: "" })
  property var statePaths: ({})
  property int backupFiles: 0
  property string daemonVersion: ""

  readonly property bool ready: connected && engine.state === "running"
  readonly property bool attention: summary.attention > 0 || (everConnected && !connected)
                                    || (connected && engine.state !== "running" && engine.state !== "starting"
                                        && engine.state !== "restarting")
  // "online": everything requested is healthy; "offline": nothing or part streaming, or no network;
  // "error": something needs attention
  readonly property string statusState: {
    if (attention || actionFailed) return "error"
    if (!connected || !online) return "offline"
    if (hasDrives && (summary.streaming > 0 || summary.folders > 0) && summary.mounted === summary.streaming) return "online"
    return "offline"
  }
  readonly property string statusText: {
    if (!connected) return starting ? "Starting the background service…" : (startError || "Connecting…")
    if (engine.state === "missing") return "rclone is not installed"
    if (engine.state !== "running") return "Starting rclone…"
    if (!hasDrives) return "No drives yet"
    var parts = []
    if (summary.streaming > 0) parts.push(summary.mounted + "/" + summary.streaming + " streaming")
    else parts.push("not streaming")
    if (summary.folders > 0) parts.push(summary.folders + (summary.folders === 1 ? " folder local" : " folders local"))
    if (summary.syncing > 0) parts.push("syncing")
    else if (summary.uploads > 0) parts.push(summary.uploads + " uploading")
    if (!online) parts.push("offline")
    return parts.join(" · ")
  }
  readonly property bool hasDrives: drives.length > 0
  readonly property bool busy: summary.syncing > 0 || transfers.count > 0 || summary.uploads > 0
  readonly property bool allStreaming: hasDrives && summary.streaming === drives.length

  // ---- transient feedback for the panel's status line
  property string lastAction: ""
  property string lastError: ""
  property string lastErrorCode: ""
  property var lastErrorArgs: null
  property bool actionFailed: false
  property int pendingRequests: 0

  signal stateUpdated()
  signal replied(int id, bool ok, var data, string error)

  property int _nextId: 1
  property var _callbacks: ({})

  function request(cmd, args, callback) {
    var s = sockLoader.item
    if (!s || !s.connected) {
      if (callback) callback(false, null, "The background service isn't running yet", "")
      ensureDaemon()
      return -1
    }
    var id = _nextId++
    if (callback) _callbacks[id] = callback
    pendingRequests++
    s.write(JSON.stringify({ id: id, cmd: cmd, args: args || {} }) + "\n")
    s.flush()
    return id
  }

  // A user-facing action: show progress text, then the daemon's message or error
  function act(cmd, args, workingText, onDone) {
    if (workingText) lastAction = workingText
    lastError = ""
    lastErrorCode = ""
    request(cmd, args, function(ok, data, error, code) {
      if (ok) {
        actionFailed = false
        var msg = data && data.message ? String(data.message) : ""
        lastAction = msg
        if (msg !== "") noticeTimer.restart()
        else lastAction = ""
      } else {
        lastAction = ""
        lastError = error || "Command failed"
        lastErrorCode = code || ""
        lastErrorArgs = { cmd: cmd, args: args }
        actionFailed = true
        actionFailedTimer.restart()
      }
      if (onDone) onDone(ok, data, error, code)
    })
  }

  function notice(text) {
    lastError = ""
    lastAction = text
    noticeTimer.restart()
  }

  function clearError() {
    lastError = ""
    lastErrorCode = ""
    actionFailed = false
  }

  function _onLine(line) {
    if (!line || line.length === 0) return
    var msg
    try {
      msg = JSON.parse(line)
    } catch (e) {
      return
    }
    if (msg.type === "state") {
      _applyState(msg.data || {})
    } else if (msg.type === "reply") {
      pendingRequests = Math.max(0, pendingRequests - 1)
      var cb = _callbacks[msg.id]
      if (cb) {
        delete _callbacks[msg.id]
        cb(msg.ok === true, msg.data || {}, String(msg.error || ""), String(msg.code || ""))
      }
      replied(Number(msg.id), msg.ok === true, msg.data || {}, String(msg.error || ""))
    }
  }

  function _applyState(s) {
    engine = s.engine || engine
    online = s.online !== false
    mountRoot = String(s.mountRoot || mountRoot)
    localRoot = String(s.localRoot || localRoot)
    cloudRoot = String(s.cloudRoot || mountRoot)
    appSettings = s.settings || appSettings
    drives = s.drives || []
    summary = s.summary || summary
    transfers = s.transfers || transfers
    recent = s.recent || []
    auth = s.auth || auth
    statePaths = s.paths || statePaths
    backupFiles = Number(s.backupFiles || 0)
    daemonVersion = String(s.version || "")
    stateUpdated()
  }

  function driveByName(name) {
    for (var i = 0; i < drives.length; i++) {
      if (drives[i].name === name) return drives[i]
    }
    return null
  }

  // ---- commands

  function setStream(remote, on, force) {
    act("stream", { remote: remote, on: on, force: force === true },
        (on ? "Connecting " : "Disconnecting ") + remote + "…")
  }
  function streamAll(on) {
    act("stream_all", { on: on }, on ? "Connecting all drives…" : "Disconnecting all drives…")
  }
  function retryDrive(remote) { act("retry", { remote: remote }, "Retrying " + remote + "…") }
  function takeOver(remote) { act("takeover", { remote: remote }, "Taking over " + remote + "…") }
  function reconnect(remote) { act("reconnect", { remote: remote }, "Opening your browser to sign in…") }
  function setDrive(remote, label, mountPath) {
    var args = { remote: remote }
    if (label !== undefined && label !== null) args.label = label
    if (mountPath !== undefined && mountPath !== null) args.mount_path = mountPath
    act("set_drive", args, "Saving " + remote + "…")
  }
  function removeRemote(remote) { act("remove_remote", { remote: remote }, "Removing " + remote + "…") }
  function setClientId(remote, clientId, clientSecret, onDone) {
    act("set_client_id", { remote: remote, client_id: clientId, client_secret: clientSecret },
        "Opening your browser to sign in with your own client…", onDone)
  }
  // Steps to create an own OAuth client: by provider id, or by an existing drive's name
  function clientGuide(provider, remote, callback) {
    request("client_guide", remote ? { remote: remote } : { provider: provider }, function(ok, data, error) {
      callback(ok ? data.guide : null, error)
    })
  }
  function dismissHint(remote, code) { request("dismiss_hint", { remote: remote, code: code }) }
  function fetchLog(source, lines, callback) {
    request("log", { source: source, lines: lines }, function(ok, data, error) {
      callback(ok ? data.lines : null, ok ? data.path : "", error)
    })
  }

  function addFolder(remote, path, local, sizeBytes, onDone) {
    act("add_folder", { remote: remote, path: path, local: local, size_bytes: sizeBytes || 0 },
        "Keeping " + (path || remote) + " on this device…", onDone)
  }
  function removeFolder(id, deleteLocal) {
    act("remove_folder", { id: id, delete_local: deleteLocal === true }, "Updating…")
  }
  function pauseFolder(id, paused) { act("pause_folder", { id: id, paused: paused }, "") }
  function syncNow(id) { act("sync_now", id ? { id: id } : {}, "") }
  function resolveFolder(id, action) { act("resolve", { id: id, action: action }, "") }
  function clearConflicts(id) { request("clear_conflicts", { id: id }) }

  function setSetting(key, value) { act("set_setting", { key: key, value: value }, "Saving…") }
  function touch() { request("touch", {}) }
  function restartEngine() { act("restart_engine", {}, "Restarting rclone…") }

  function addOAuth(name, provider, clientId, clientSecret, mountPath, stream) {
    act("add_oauth", { name: name, provider: provider, client_id: clientId || "", client_secret: clientSecret || "",
                       mount_path: mountPath || "", stream: stream !== false }, "")
  }
  function addCredentials(name, provider, options, mountPath, stream) {
    act("add_credentials", { name: name, provider: provider, options: options, mount_path: mountPath || "",
                             stream: stream !== false }, "")
  }
  function cancelAuth() { request("cancel_auth", {}) }
  function dismissAuth() { request("dismiss_auth", {}) }

  // Files, folders and URLs. gio, unlike xdg-open outside GNOME/KDE, runs terminal apps (nvim
  // for text files) in a terminal; without one they would start invisibly and never exit.
  // Args stay positional, never parsed by the shell.
  function openPath(path) {
    if (!path) return
    Quickshell.execDetached(["bash", "-lc", 'gio open "$1" 2>/dev/null || exec xdg-open "$1"', "bash", String(path)])
  }
  function openUrl(url) {
    if (url) openPath(url)
  }
  function copyText(text) {
    Quickshell.execDetached(["bash", "-c", 'printf %s "$1" | wl-copy', "bash", String(text || "")])
  }

  // ---- connection management
  //
  // Quickshell's Socket never retries after a failed connect, so every attempt gets a fresh
  // Socket (recreated by the Loader), which connects once its signal handlers are attached.

  function connectNow() {
    if (connected) return
    sockLoader.active = false
    sockLoader.active = true
  }

  function ensureDaemon() {
    if (ensureProc.running || pluginDir === "") return
    starting = true
    startError = ""
    ensureProc.command = ["python3", cli, "daemon", "--ensure"]
    ensureProc.running = true
  }

  function _onSocketState(isConnected) {
    if (isConnected === connected) return
    connected = isConnected
    if (isConnected) {
      everConnected = true
      starting = false
      startError = ""
      reconnectTimer.attempts = 0
      reconnectTimer.stop()
      return
    }
    // Replies to requests in flight will never come
    var cbs = _callbacks
    _callbacks = ({})
    pendingRequests = 0
    for (var id in cbs) cbs[id](false, null, "Lost connection to the background service", "")
    reconnectTimer.restart()
  }

  Loader {
    id: sockLoader
    active: false
    sourceComponent: Component {
      Socket {
        id: sock
        path: root.socketPath
        parser: SplitParser {
          onRead: function(data) { root._onLine(data) }
        }
        onConnectionStateChanged: root._onSocketState(sock.connected)
        onError: function(error) {
          if (!sock.connected && !reconnectTimer.running) reconnectTimer.restart()
        }
        Component.onCompleted: sock.connected = true
      }
    }
  }

  Timer {
    id: reconnectTimer
    property int attempts: 0
    // Quick retries right after startup, then back off
    interval: attempts < 10 ? 500 : 5000
    onTriggered: {
      attempts++
      if (attempts === 2) root.ensureDaemon()
      root.connectNow()
    }
  }

  Process {
    id: ensureProc
    running: false
    stdout: StdioCollector { id: ensureOut }
    stderr: StdioCollector { id: ensureErr }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.starting = false
        root.startError = String(ensureErr.text || ensureOut.text || "Could not start guacd").trim()
      }
      root.connectNow()
    }
  }

  Timer {
    id: noticeTimer
    interval: 4000
    onTriggered: root.lastAction = ""
  }

  Timer {
    id: actionFailedTimer
    interval: 30000
    onTriggered: root.actionFailed = false
  }

  Component.onCompleted: connectNow()
}
