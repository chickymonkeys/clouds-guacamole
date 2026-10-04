import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Browse a drive's folders and choose one to keep on this device.
Item {
  id: root

  property var service: null
  property string remote: ""
  property string driveLabel: remote
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string home: ""

  property string path: ""
  property var dirs: []
  property bool loading: false
  property string error: ""
  property bool keptHere: false
  property bool containsKept: false
  property bool truncated: false
  property string suggestedLocal: ""
  property bool localEdited: false
  property int sizeRequest: 0
  property bool sizeLoading: false
  property double sizeBytes: 0
  property int sizeCount: -1
  property bool submitting: false

  signal done()
  signal cancelled()

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property var crumbs: path === "" ? [] : path.split("/")
  readonly property string folderName: crumbs.length > 0 ? crumbs[crumbs.length - 1] : driveLabel
  readonly property bool canKeep: !keptHere && !containsKept && !loading && !submitting && localField.text.trim() !== ""

  implicitHeight: col.implicitHeight

  function open(remoteName, label) {
    remote = remoteName
    driveLabel = label || remoteName
    localEdited = false
    navigate("")
  }

  function navigate(newPath) {
    path = newPath
    loading = true
    error = ""
    dirs = []
    sizeCount = -1
    sizeBytes = 0
    var requested = newPath
    service.request("browse", { remote: remote, path: newPath }, function(ok, data, err) {
      if (requested !== root.path) return
      root.loading = false
      if (!ok) {
        root.error = err
        return
      }
      root.dirs = data.dirs || []
      root.keptHere = data.keptHere === true
      root.containsKept = data.containsKept === true
      root.truncated = data.truncated === true
      root.suggestedLocal = String(data.suggestedLocal || "")
      if (!root.localEdited) localField.text = Model.tildify(root.suggestedLocal, root.home)
      root.measure()
    })
  }

  function measure() {
    if (keptHere || containsKept) return
    var id = ++sizeRequest
    sizeLoading = true
    service.request("folder_size", { remote: remote, path: path, timeout: 30 }, function(ok, data) {
      if (id !== root.sizeRequest) return
      root.sizeLoading = false
      if (ok) {
        root.sizeBytes = Number(data.bytes || 0)
        root.sizeCount = Number(data.count || 0)
      }
    })
  }

  function keep() {
    submitting = true
    service.addFolder(remote, path, localField.text.trim(), sizeCount >= 0 ? sizeBytes : 0, function(ok) {
      root.submitting = false
      if (ok) root.done()
    })
  }

  ColumnLayout {
    id: col
    width: parent.width
    spacing: Style.space(10)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Button {
        iconText: "󰁝"
        text: "Back"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        onClicked: root.cancelled()
      }

      Text {
        Layout.fillWidth: true
        text: "Keep a folder on this device"
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        color: root.foreground
        elide: Text.ElideRight
      }
    }

    Text {
      Layout.fillWidth: true
      text: "Folders kept on this device open instantly and work offline. Changes sync both ways within seconds; conflicts never lose data."
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      color: root.dim
      wrapMode: Text.WordWrap
    }

    // Breadcrumb
    Flow {
      Layout.fillWidth: true
      spacing: Style.space(2)

      Button {
        text: root.driveLabel
        iconText: "󰋊"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.path === "" ? Color.accent : root.foreground
        bordered: false
        onClicked: root.navigate("")
      }

      Repeater {
        model: root.crumbs.length

        Row {
          required property int index
          spacing: Style.space(2)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "›"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Button {
            text: root.crumbs[index]
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: index === root.crumbs.length - 1 ? Color.accent : root.foreground
            bordered: false
            onClicked: root.navigate(root.crumbs.slice(0, index + 1).join("/"))
          }
        }
      }
    }

    // Folder list
    BorderSurface {
      Layout.fillWidth: true
      implicitHeight: Math.min(Style.space(220), Math.max(Style.space(60), listCol.implicitHeight + Style.space(8)))
      radius: Style.cornerRadius
      color: Qt.rgba(1, 1, 1, 0.03)
      borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

      Flickable {
        id: listFlick
        anchors.fill: parent
        anchors.margins: Style.space(4)
        contentWidth: width
        contentHeight: listCol.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: listCol
          width: listFlick.width

          Text {
            visible: root.loading || root.error !== "" || (root.dirs.length === 0)
            width: parent.width
            padding: Style.space(8)
            textFormat: Text.PlainText
            text: root.loading ? "Loading folders…" : (root.error !== "" ? root.error : "No subfolders here")
            color: root.error !== "" ? Color.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.loading ? [] : root.dirs

            CursorSurface {
              required property var modelData
              width: listCol.width
              implicitHeight: Style.space(28)
              hasCursor: dirMouse.containsMouse

              MouseArea {
                id: dirMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.navigate(modelData.path)
              }

              RowLayout {
                anchors {
                  fill: parent
                  leftMargin: Style.space(6)
                  rightMargin: Style.space(6)
                }
                spacing: Style.space(8)

                Text {
                  text: modelData.kept ? "󰋊" : "󰉋"
                  color: modelData.kept ? Color.accent : Qt.darker(root.foreground, 1.2)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }

                Text {
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  text: modelData.name
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }

                Text {
                  visible: modelData.kept || modelData.containsKept
                  text: modelData.kept ? "on this device" : "has a local folder"
                  color: Color.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption - Style.space(1)
                }

                Text {
                  text: "󰅂"
                  color: dirMouse.containsMouse ? Color.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }

          Text {
            visible: root.truncated
            width: parent.width
            padding: Style.space(6)
            text: "Showing the first 2000 folders"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption - Style.space(1)
          }
        }
      }
    }

    // Choose this folder
    BorderSurface {
      Layout.fillWidth: true
      implicitHeight: keepCol.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.alpha(Color.accent, 0.06)
      borderSpec: Border.controlSpec("focus", root.foreground, Color.accent)

      ColumnLayout {
        id: keepCol
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.path === "" ? "Keep all of " + root.driveLabel + " on this device"
                                 : "Keep “" + root.folderName + "” on this device"
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          color: root.foreground
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          visible: text !== ""
          text: {
            if (root.keptHere) return "This folder is already kept on this device (or is inside one that is)."
            if (root.containsKept) return "A folder inside this one is already kept locally; pick that one or a folder elsewhere."
            if (root.sizeLoading) return "Measuring size…"
            if (root.sizeCount >= 0) return Model.formatBytes(root.sizeBytes) + " in " + root.sizeCount + (root.sizeCount === 1 ? " file" : " files") + " will be downloaded"
            return ""
          }
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          color: root.keptHere || root.containsKept ? Color.urgent : root.dim
          wrapMode: Text.WordWrap
        }

        Text {
          text: "Local folder"
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(1)
          color: Qt.darker(root.foreground, 1.4)
        }

        TextField {
          id: localField
          Layout.fillWidth: true
          placeholderText: Model.tildify(root.suggestedLocal, root.home)
          onTextEdited: root.localEdited = true
          onAccepted: if (root.canKeep) root.keep()
        }

        Text {
          Layout.fillWidth: true
          text: "If this folder already has files, both sides are merged (the newer copy of each file wins). Local files a sync replaces are kept for "
                + (root.service && root.service.appSettings.backup_days ? root.service.appSettings.backup_days : 30) + " days."
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - Style.space(2)
          color: root.dim
          wrapMode: Text.WordWrap
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(8)

          Button {
            iconText: "󰋊"
            text: root.submitting ? "Setting up…" : "Keep on this device"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            enabled: root.canKeep
            opacity: enabled ? 1 : 0.45
            onClicked: root.keep()
          }

          Item { Layout.fillWidth: true }

          Button {
            visible: root.path !== ""
            iconText: "󰁝"
            text: "Up"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: false
            onClicked: root.navigate(root.crumbs.slice(0, root.crumbs.length - 1).join("/"))
          }
        }
      }
    }
  }
}
