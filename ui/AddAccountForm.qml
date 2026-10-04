import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Connect a new cloud account without a terminal. Browser sign-in for OAuth providers,
// a short form for the rest. Secrets go to the daemon over its private socket only.
Item {
  id: root

  property var backend: null
  property color foreground: Color.foreground
  property color urgent: Color.urgent
  property string fontFamily: Style.font.family

  property var selectedProvider: null
  property string remoteName: ""
  property string mountPath: ""
  property bool streamNow: true
  property bool showPassword: false
  property bool showOwnClient: false
  property string s3Preset: "AWS"
  property bool submitted: false

  signal accountAdded(string remoteName)
  signal cancelled()

  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property var auth: backend ? backend.auth : ({})
  readonly property bool working: auth.busy === true && auth.remote === remoteName.trim()
  readonly property bool waitingBrowser: working && auth.waiting === true

  implicitWidth: parent ? parent.width : Style.space(380)
  implicitHeight: mainCol.implicitHeight

  function reset() {
    selectedProvider = null
    submitted = false
    showOwnClient = false
    if (backend) backend.dismissAuth()
  }

  function selectProvider(provId) {
    var p = provId ? Model.getProvider(provId) : null
    selectedProvider = p
    submitted = false
    if (!p) return
    // rclone's built-in clients are shared by every rclone user (and Google's is retiring):
    // every provider that can use an own client starts with the guided setup
    showOwnClient = p.customClient === true
    if (p.customClient) ownGuide.load(p.id, "")
    var base = p.defaultName
    var name = base
    var n = 2
    var taken = {}
    if (backend) for (var i = 0; i < backend.drives.length; i++) taken[backend.drives[i].name] = true
    while (taken[name]) name = base + "_" + (n++)
    remoteName = name
    mountPath = (backend ? backend.mountRoot : "~/Cloud/Stream") + "/" + name
    if (backend) backend.dismissAuth()
  }

  function submitOAuth() {
    submitted = true
    var own = showOwnClient && selectedProvider.customClient === true
    backend.addOAuth(remoteName.trim(), selectedProvider.id,
                     own ? ownGuide.clientId : "", own ? ownGuide.clientSecret : "",
                     mountPath.trim(), streamNow)
  }

  function submitCredentials(options) {
    submitted = true
    backend.addCredentials(remoteName.trim(), selectedProvider.id, options, mountPath.trim(), streamNow)
  }

  Connections {
    target: root.backend
    function onStateUpdated() {
      if (root.submitted && root.auth.success && root.auth.remote === root.remoteName.trim()) {
        successTimer.restart()
      }
    }
  }

  Timer {
    id: successTimer
    interval: 1200
    onTriggered: {
      var name = root.remoteName.trim()
      root.reset()
      root.accountAdded(name)
    }
  }

  component FieldLabel: Text {
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    color: root.foreground
  }

  ColumnLayout {
    id: mainCol
    width: parent.width
    spacing: Style.space(10)

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(8)

      Button {
        iconText: "󰁍"
        text: root.selectedProvider ? "Providers" : "Back"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: root.foreground
        bordered: true
        onClicked: {
          if (root.waitingBrowser) root.backend.cancelAuth()
          if (root.selectedProvider) root.selectProvider("")
          else root.cancelled()
        }
      }

      Text {
        Layout.fillWidth: true
        text: root.selectedProvider ? root.selectedProvider.name : "Connect a cloud drive"
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        color: root.foreground
        elide: Text.ElideRight
      }

      Text {
        visible: root.selectedProvider !== null
        text: root.selectedProvider ? root.selectedProvider.glyph : ""
        color: root.selectedProvider ? root.selectedProvider.color : Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
      }
    }

    // Result banners
    BorderSurface {
      visible: root.submitted && (root.auth.success || "") !== "" && root.auth.remote === root.remoteName.trim()
      Layout.fillWidth: true
      implicitHeight: okText.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.alpha(Color.accent, 0.12)
      borderSpec: Border.controlSpec("normal", Color.accent, Color.accent)

      Text {
        id: okText
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        textFormat: Text.PlainText
        text: "󰄬  " + (root.auth.success || "")
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        color: root.foreground
        wrapMode: Text.WordWrap
      }
    }

    BorderSurface {
      visible: root.submitted && !root.working && (root.auth.error || "") !== "" && root.auth.remote === root.remoteName.trim()
      Layout.fillWidth: true
      implicitHeight: errText.implicitHeight + Style.space(16)
      radius: Style.cornerRadius
      color: Qt.alpha(root.urgent, 0.12)
      borderSpec: Border.controlSpec("normal", root.urgent, root.urgent)

      Text {
        id: errText
        anchors {
          fill: parent
          margins: Style.space(8)
        }
        textFormat: Text.PlainText
        text: "󰅚  " + (root.auth.error || "")
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        color: root.foreground
        wrapMode: Text.WordWrap
      }
    }

    // ---- provider list
    ColumnLayout {
      visible: root.selectedProvider === null
      Layout.fillWidth: true
      spacing: Style.space(4)

      Repeater {
        model: Model.ALL_PROVIDERS

        CursorSurface {
          required property var modelData
          Layout.fillWidth: true
          implicitHeight: Style.space(44)
          radius: Style.cornerRadius
          hasCursor: provMouse.containsMouse

          MouseArea {
            id: provMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.selectProvider(modelData.id)
          }

          RowLayout {
            anchors {
              fill: parent
              leftMargin: Style.space(8)
              rightMargin: Style.space(8)
            }
            spacing: Style.space(10)

            Rectangle {
              implicitWidth: Style.space(30)
              implicitHeight: Style.space(30)
              radius: Style.cornerRadius
              color: Qt.alpha(modelData.color, 0.15)

              Text {
                anchors.centerIn: parent
                text: modelData.glyph
                color: modelData.color
                font.family: root.fontFamily
                font.pixelSize: Style.font.icon
              }
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0

              Text {
                Layout.fillWidth: true
                text: modelData.name
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                color: root.foreground
                elide: Text.ElideRight
              }

              Text {
                text: modelData.authTag + " · " + modelData.category
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption - Style.space(2)
                color: root.dim
              }
            }

            Text {
              text: "󰅂"
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              color: provMouse.containsMouse ? Color.accent : root.dim
            }
          }
        }
      }
    }

    // ---- configuration form
    ColumnLayout {
      visible: root.selectedProvider !== null
      Layout.fillWidth: true
      spacing: Style.space(10)

      BorderSurface {
        Layout.fillWidth: true
        implicitHeight: idCol.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: Qt.rgba(1, 1, 1, 0.03)
        borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

        ColumnLayout {
          id: idCol
          anchors {
            fill: parent
            margins: Style.space(10)
          }
          spacing: Style.space(6)

          FieldLabel { text: "Name" }
          TextField {
            id: nameField
            Layout.fillWidth: true
            text: root.remoteName
            placeholderText: "e.g. Work"
            onTextEdited: {
              var cleaned = text.replace(/[^A-Za-z0-9_-]/g, "").replace(/^-+/, "")
              if (cleaned !== text) text = cleaned
              root.remoteName = cleaned
              if (!mountField.activeFocus) {
                root.mountPath = (root.backend ? root.backend.mountRoot : "~/Cloud/Stream") + "/" + cleaned
              }
            }
          }

          FieldLabel { text: "Stream into" }
          TextField {
            id: mountField
            Layout.fillWidth: true
            text: root.mountPath
            onTextEdited: root.mountPath = text
          }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)

            Text {
              Layout.fillWidth: true
              text: "Stream it right away"
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              color: root.foreground
            }

            ToggleSwitch {
              checked: root.streamNow
              foreground: root.foreground
              onToggled: root.streamNow = !root.streamNow
            }
          }
        }
      }

      // A. OAuth providers
      ColumnLayout {
        visible: root.selectedProvider !== null && root.selectedProvider.authType === "oauth"
        Layout.fillWidth: true
        spacing: Style.space(8)

        // Own OAuth client, set up step by step: needed for Google Drive, which loses rclone's
        // shared client during 2026, and recommended for the others
        BorderSurface {
          visible: root.selectedProvider !== null && root.selectedProvider.customClient === true
          Layout.fillWidth: true
          implicitHeight: ownCol.implicitHeight + Style.space(20)
          radius: Style.cornerRadius
          color: Qt.rgba(1, 1, 1, 0.03)
          borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

          ColumnLayout {
            id: ownCol
            anchors {
              fill: parent
              margins: Style.space(10)
            }
            spacing: Style.space(8)

            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(8)

              Text {
                Layout.fillWidth: true
                text: !root.selectedProvider ? ""
                      : "Use my own " + root.selectedProvider.name + " client"
                        + (root.selectedProvider.id === "drive" ? " (needed)" : " (recommended)")
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                color: root.foreground
                wrapMode: Text.WordWrap
              }

              ToggleSwitch {
                checked: root.showOwnClient
                foreground: root.foreground
                onToggled: root.showOwnClient = !root.showOwnClient
              }
            }

            Text {
              visible: !root.showOwnClient
              Layout.fillWidth: true
              text: !root.selectedProvider ? ""
                    : root.selectedProvider.id === "drive"
                      ? "rclone's shared Google client is throttled across all its users and stops working during 2026: this drive would be slow, then stop."
                      : "rclone's built-in " + root.selectedProvider.name + " app is used instead, shared by every rclone user and throttled with them. You can set up your own later from the drive card."
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption - Style.space(1)
              color: root.urgent
              wrapMode: Text.WordWrap
            }

            ClientGuide {
              id: ownGuide
              visible: root.showOwnClient
              Layout.fillWidth: true
              service: root.backend
              foreground: root.foreground
              fontFamily: root.fontFamily
            }
          }
        }

        BorderSurface {
          visible: root.waitingBrowser
          Layout.fillWidth: true
          implicitHeight: waitCol.implicitHeight + Style.space(16)
          radius: Style.cornerRadius
          color: Qt.alpha(Color.accent, 0.1)
          borderSpec: Border.controlSpec("focus", root.foreground, Color.accent)

          ColumnLayout {
            id: waitCol
            anchors {
              fill: parent
              margins: Style.space(10)
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

            Text {
              Layout.fillWidth: true
              text: "Guacamole notices when you are done."
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              color: root.dim
              wrapMode: Text.WordWrap
            }

            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(6)

              Button {
                visible: (root.auth.url || "") !== ""
                iconText: "󰖟"
                text: "Open sign-in page"
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.foreground
                bordered: true
                onClicked: root.backend.openUrl(root.auth.url)
              }

              Item { Layout.fillWidth: true }

              Button {
                text: "Cancel"
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                foreground: root.urgent
                bordered: false
                onClicked: root.backend.cancelAuth()
              }
            }
          }
        }

        Button {
          readonly property bool ownClient: root.showOwnClient && root.selectedProvider !== null
                                            && root.selectedProvider.customClient === true
          // With an own client, signing in comes after its last step
          visible: !root.waitingBrowser && (!ownClient || ownGuide.atFields)
          Layout.fillWidth: true
          implicitHeight: Style.space(38)
          text: ownClient ? "Sign in with my client" : "Sign in with the browser"
          iconText: root.selectedProvider ? root.selectedProvider.glyph : "󰌋"
          fontFamily: root.fontFamily
          foreground: root.foreground
          bordered: true
          enabled: root.remoteName.trim() !== "" && !root.working && (!ownClient || ownGuide.valid)
          opacity: enabled ? 1 : 0.45
          onClicked: root.submitOAuth()
        }
      }

      // B. Nextcloud / WebDAV
      ColumnLayout {
        visible: root.selectedProvider !== null && (root.selectedProvider.id === "nextcloud" || root.selectedProvider.id === "webdav")
        Layout.fillWidth: true
        spacing: Style.space(6)

        FieldLabel { text: root.selectedProvider && root.selectedProvider.id === "webdav" ? "WebDAV URL" : "Server address" }
        TextField {
          id: davUrl
          Layout.fillWidth: true
          placeholderText: root.selectedProvider && root.selectedProvider.id === "webdav" ? "https://dav.example.com/remote.php/webdav" : "https://cloud.example.com"
        }

        FieldLabel { text: "Username" }
        TextField {
          id: davUser
          Layout.fillWidth: true
          placeholderText: "username"
        }

        RowLayout {
          Layout.fillWidth: true
          FieldLabel { text: root.selectedProvider && root.selectedProvider.id === "nextcloud" ? "App password" : "Password" }
          Item { Layout.fillWidth: true }
          Button {
            text: root.showPassword ? "Hide" : "Show"
            fontFamily: root.fontFamily
            fontSize: Style.font.caption - Style.space(2)
            foreground: root.dim
            bordered: false
            onClicked: root.showPassword = !root.showPassword
          }
        }
        TextField {
          id: davPass
          Layout.fillWidth: true
          placeholderText: "Password or app token"
          password: !root.showPassword
        }

        Button {
          Layout.fillWidth: true
          implicitHeight: Style.space(38)
          text: root.working ? "Connecting…" : "Connect"
          iconText: "󰒋"
          fontFamily: root.fontFamily
          foreground: root.foreground
          bordered: true
          enabled: !root.working && root.remoteName.trim() !== "" && davUrl.text.trim() !== ""
          opacity: enabled ? 1 : 0.45
          onClicked: root.submitCredentials({ url: davUrl.text.trim(), user: davUser.text.trim(), pass: davPass.text })
        }
      }

      // C. S3-compatible
      ColumnLayout {
        visible: root.selectedProvider !== null && root.selectedProvider.id === "s3"
        Layout.fillWidth: true
        spacing: Style.space(6)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(4)

          Repeater {
            model: [{ v: "AWS", t: "AWS" }, { v: "Minio", t: "MinIO" }, { v: "Cloudflare", t: "R2" }, { v: "Other", t: "Other" }]

            Button {
              required property var modelData
              text: modelData.t
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              selected: root.s3Preset === modelData.v
              foreground: root.foreground
              bordered: true
              onClicked: root.s3Preset = modelData.v
            }
          }
        }

        FieldLabel { text: "Endpoint (optional for AWS)" }
        TextField {
          id: s3Endpoint
          Layout.fillWidth: true
          placeholderText: root.s3Preset === "Cloudflare" ? "https://<account>.r2.cloudflarestorage.com" : "https://s3.example.com"
        }

        FieldLabel { text: "Access key ID" }
        TextField {
          id: s3Key
          Layout.fillWidth: true
        }

        FieldLabel { text: "Secret access key" }
        TextField {
          id: s3Secret
          Layout.fillWidth: true
          password: !root.showPassword
        }

        Button {
          Layout.fillWidth: true
          implicitHeight: Style.space(38)
          text: root.working ? "Connecting…" : "Connect"
          iconText: "󰋊"
          fontFamily: root.fontFamily
          foreground: root.foreground
          bordered: true
          enabled: !root.working && root.remoteName.trim() !== "" && s3Key.text.trim() !== "" && s3Secret.text.trim() !== ""
          opacity: enabled ? 1 : 0.45
          onClicked: root.submitCredentials({ provider: root.s3Preset, endpoint: s3Endpoint.text.trim(),
                                              access_key_id: s3Key.text.trim(), secret_access_key: s3Secret.text.trim() })
        }
      }

      // D. Proton Drive
      ColumnLayout {
        visible: root.selectedProvider !== null && root.selectedProvider.id === "protondrive"
        Layout.fillWidth: true
        spacing: Style.space(6)

        FieldLabel { text: "Proton email" }
        TextField {
          id: protonUser
          Layout.fillWidth: true
          placeholderText: "you@proton.me"
        }

        FieldLabel { text: "Password" }
        TextField {
          id: protonPass
          Layout.fillWidth: true
          password: !root.showPassword
        }

        FieldLabel { text: "2FA code (if enabled)" }
        TextField {
          id: proton2fa
          Layout.fillWidth: true
          placeholderText: "123456"
        }

        Button {
          Layout.fillWidth: true
          implicitHeight: Style.space(38)
          text: root.working ? "Connecting…" : "Connect"
          iconText: "󰅟"
          fontFamily: root.fontFamily
          foreground: root.foreground
          bordered: true
          enabled: !root.working && root.remoteName.trim() !== "" && protonUser.text.trim() !== "" && protonPass.text !== ""
          opacity: enabled ? 1 : 0.45
          onClicked: root.submitCredentials({ username: protonUser.text.trim(), password: protonPass.text, "2fa": proton2fa.text.trim() })
        }
      }
    }
  }
}
