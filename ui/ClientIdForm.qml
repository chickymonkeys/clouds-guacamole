import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Sign an existing drive in again with the user's own OAuth client, after a guided setup of
// that client. For Google Drive this replaces rclone's shared client, which is throttled across
// every rclone user and stops working during 2026.
Item {
  id: root

  property var service: null
  property string remote: ""
  property string providerName: ""
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property bool submitted: false

  signal done()
  signal cancelled()

  readonly property var auth: service ? service.auth : ({})
  readonly property bool waiting: auth.busy === true && auth.remote === remote

  implicitHeight: col.implicitHeight

  function open(remoteName, provName) {
    remote = remoteName
    providerName = provName
    submitted = false
    if (service) service.dismissAuth()
    guide.load("", remoteName)
  }

  ColumnLayout {
    id: col
    width: parent.width
    spacing: Style.space(10)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Button {
        iconText: "󰁍"
        text: "Back"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        onClicked: {
          if (root.waiting) root.service.cancelAuth()
          root.cancelled()
        }
      }

      Text {
        Layout.fillWidth: true
        textFormat: Text.PlainText
        text: (guide.guide ? guide.guide.title : "Your own client") + " · " + root.remote
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        color: root.foreground
        elide: Text.ElideRight
      }
    }

    ClientGuide {
      id: guide
      Layout.fillWidth: true
      service: root.service
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    // Waiting for the browser
    BorderSurface {
      visible: root.waiting
      Layout.fillWidth: true
      implicitHeight: waitCol.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.alpha(Color.accent, 0.1)
      borderSpec: Border.controlSpec("focus", root.foreground, Color.accent)

      ColumnLayout {
        id: waitCol
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        spacing: Style.space(6)

        Text {
          Layout.fillWidth: true
          text: "Waiting for you to sign in in the browser…"
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          color: root.foreground
          wrapMode: Text.WordWrap
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Button {
            visible: (root.auth.url || "") !== ""
            text: "Open sign-in page"
            iconText: "󰖟"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: root.foreground
            bordered: true
            onClicked: root.service.openUrl(root.auth.url)
          }

          Item { Layout.fillWidth: true }

          Button {
            text: "Cancel"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            foreground: Color.urgent
            bordered: false
            onClicked: root.service.cancelAuth()
          }
        }
      }
    }

    Text {
      // Sign-in failures arrive in auth; a rejected client never starts one
      readonly property string message: !root.submitted || root.waiting ? ""
                                        : ((root.auth.error || "") !== "" && root.auth.remote === root.remote
                                           ? root.auth.error : root.service.lastError)
      visible: message !== ""
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: message
      color: Color.urgent
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
    }

    Button {
      visible: guide.atFields && !root.waiting
      Layout.fillWidth: true
      implicitHeight: Style.space(36)
      iconText: "󰌋"
      text: "Sign in with my client"
      fontFamily: root.fontFamily
      foreground: root.foreground
      bordered: true
      enabled: guide.valid
      opacity: enabled ? 1 : 0.45
      onClicked: {
        root.submitted = true
        root.service.setClientId(root.remote, guide.clientId, guide.clientSecret, function(ok) {
          if (ok) {
            guide.clearSecret()
            root.done()
          }
        })
      }
    }
  }
}
