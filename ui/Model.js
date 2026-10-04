.pragma library

// Formatters and static data for the Clouds Guacamole widget

function formatBytes(bytes) {
  var b = Number(bytes || 0)
  if (b <= 0 || !isFinite(b)) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB", "PB"]
  var i = 0
  while (b >= 1024 && i < units.length - 1) {
    b /= 1024
    i++
  }
  // 100 GB, not 100.0 GB
  return (i === 0 ? b.toFixed(0) : b.toFixed(1).replace(/\.0$/, "")) + " " + units[i]
}

function formatSpeed(bps) {
  var n = Number(bps || 0)
  return n > 0 ? formatBytes(n) + "/s" : ""
}

function ago(ts) {
  var t = Number(ts || 0)
  if (t <= 0) return "never"
  var diff = Math.max(0, Math.floor(Date.now() / 1000) - t)
  if (diff < 45) return "just now"
  var mins = Math.floor(diff / 60)
  if (mins < 60) return Math.max(1, mins) + "m ago"
  var hours = Math.floor(mins / 60)
  if (hours < 24) return hours + "h ago"
  var days = Math.floor(hours / 24)
  if (days < 30) return days + "d ago"
  return Math.floor(days / 30) + "mo ago"
}

function inFuture(ts) {
  var diff = Number(ts || 0) - Math.floor(Date.now() / 1000)
  if (diff <= 5) return "soon"
  if (diff < 60) return "in " + diff + "s"
  var mins = Math.round(diff / 60)
  if (mins < 60) return "in " + mins + "m"
  return "in " + Math.round(mins / 60) + "h"
}

// Collapse $HOME to ~ for display
function tildify(path, home) {
  var p = String(path || "")
  if (home && (p === home || p.indexOf(home + "/") === 0)) return "~" + p.substring(home.length)
  return p
}

function mountStateText(d) {
  if (!d) return ""
  switch (d.mountState) {
    case "mounted": return "streaming"
    case "mounting": return "connecting…"
    case "unmounting": return "disconnecting…"
    case "waiting": return d.errorKind === "rate" ? "rate-limited" : "waiting for network"
    case "error": return "error"
    case "foreign": return "in use elsewhere"
    default: return d.stream ? "starting…" : "not streaming"
  }
}

function folderStateText(f) {
  if (!f) return ""
  switch (f.state) {
    case "syncing": {
      var p = f.progress || {}
      if (p.totalTransfers > 0) return (f.resync ? "first sync " : "syncing ") + p.transfers + "/" + p.totalTransfers
      return f.resync ? "first sync…" : "syncing…"
    }
    case "synced": return "synced " + ago(f.lastSync)
    case "changes": return "changes pending"
    case "pending": return "waiting to sync"
    case "waiting": return "offline, will sync"
    case "paused": return "paused"
    case "attention": return "needs attention"
    case "error": return "retrying " + inFuture(f.nextSync)
    default: return f.state || ""
  }
}

// Folders that stay listed while their drive's list is closed: the ones that need a look or a
// decision. Syncing doesn't count, or rows would come and go every few minutes; a folder you
// just added shows because adding one opens the list.
function folderStaysListed(f) {
  if (!f) return false
  if (f.state === "attention" || f.state === "error") return true
  return (f.conflictCount || 0) > 0 || (f.watchError || "") !== ""
}

// One phrase for a drive's folders, what the busiest of them is doing, and its tone
function folderGroup(list) {
  var attention = 0, retrying = 0, syncing = 0, pending = 0, offline = 0, paused = 0
  for (var i = 0; i < list.length; i++) {
    var f = list[i] || {}
    if (f.state === "attention") attention++
    else if (f.state === "error") retrying++
    else if (f.paused || f.state === "paused") paused++
    else if (f.state === "syncing") syncing++
    else if (f.state === "changes" || f.state === "pending") pending++
    else if (f.state === "waiting") offline++
  }
  var busy = syncing > 0
  if (attention > 0) return { text: attention + (attention === 1 ? " needs you" : " need you"), tone: "urgent", busy: busy }
  if (retrying > 0) return { text: retrying + " retrying", tone: "urgent", busy: busy }
  if (syncing > 0) return { text: syncing + " syncing", tone: "accent", busy: busy }
  if (pending > 0) return { text: pending + " waiting to sync", tone: "accent", busy: false }
  if (offline > 0) return { text: "offline, will sync", tone: "dim", busy: false }
  if (paused > 0 && paused === list.length) return { text: "paused", tone: "dim", busy: false }
  if (paused > 0) return { text: "synced, " + paused + " paused", tone: "dim", busy: false }
  return { text: list.length === 1 ? "synced" : "all synced", tone: "dim", busy: false }
}

function fileGlyph(name) {
  var n = String(name || "").toLowerCase()
  var ext = n.lastIndexOf(".") > 0 ? n.substring(n.lastIndexOf(".") + 1) : ""
  if (["png", "jpg", "jpeg", "gif", "webp", "heic", "svg", "bmp", "tiff"].indexOf(ext) !== -1) return "󰈟"
  if (["mp4", "mkv", "mov", "webm", "avi"].indexOf(ext) !== -1) return "󰈫"
  if (["mp3", "flac", "ogg", "wav", "m4a", "opus"].indexOf(ext) !== -1) return "󰈣"
  if (["pdf"].indexOf(ext) !== -1) return "󰈦"
  if (["zip", "tar", "gz", "xz", "7z", "rar", "zst"].indexOf(ext) !== -1) return "󰗄"
  if (["md", "txt", "org", "tex", "rst"].indexOf(ext) !== -1) return "󰈙"
  if (["doc", "docx", "odt", "rtf"].indexOf(ext) !== -1) return "󰈬"
  if (["xls", "xlsx", "ods", "csv"].indexOf(ext) !== -1) return "󰈛"
  if (["ppt", "pptx", "odp", "key"].indexOf(ext) !== -1) return "󰈧"
  return "󰈔"
}

var ALL_PROVIDERS = [
  { id: "drive", name: "Google Drive", category: "Cloud storage", glyph: "󰊭", color: "#4285F4",
    authType: "oauth", defaultName: "GoogleDrive", authTag: "Own client · browser sign-in", customClient: true },
  { id: "onedrive", name: "Microsoft OneDrive", category: "Cloud storage", glyph: "󰏊", color: "#0078D4",
    authType: "oauth", defaultName: "OneDrive", authTag: "Own client · browser sign-in", customClient: true },
  { id: "dropbox", name: "Dropbox", category: "Cloud storage", glyph: "\uf16b", color: "#0061FF",
    authType: "oauth", defaultName: "Dropbox", authTag: "Own client · browser sign-in", customClient: true },
  { id: "nextcloud", name: "Nextcloud / ownCloud", category: "Self-hosted", glyph: "󰒋", color: "#0082C9",
    authType: "credentials", defaultName: "Nextcloud", authTag: "Server & app password" },
  { id: "s3", name: "S3 / MinIO / R2", category: "Object storage", glyph: "󰋊", color: "#FF9900",
    authType: "credentials", defaultName: "S3", authTag: "Access key & secret" },
  { id: "box", name: "Box", category: "Cloud storage", glyph: "󰉉", color: "#0061D5",
    authType: "oauth", defaultName: "Box", authTag: "Own client · browser sign-in", customClient: true },
  { id: "pcloud", name: "pCloud", category: "Cloud storage", glyph: "󰅟", color: "#14BF96",
    authType: "oauth", defaultName: "pCloud", authTag: "Own client · browser sign-in", customClient: true },
  { id: "protondrive", name: "Proton Drive", category: "Encrypted", glyph: "󰅟", color: "#6D4AFF",
    authType: "credentials", defaultName: "ProtonDrive", authTag: "Proton login" },
  { id: "webdav", name: "WebDAV", category: "Protocol", glyph: "󰒋", color: "#7E57C2",
    authType: "credentials", defaultName: "WebDAV", authTag: "URL & login" }
]

function getProvider(id) {
  for (var i = 0; i < ALL_PROVIDERS.length; i++) {
    if (ALL_PROVIDERS[i].id === id) return ALL_PROVIDERS[i]
  }
  return null
}
