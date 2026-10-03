import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Guided creation of the user's own OAuth client, one step at a time, ending with the client
// id and secret fields. The steps come from the daemon (guac/providers.py, after rclone's docs)
// so the widget and `guac set-client-id` say the same thing. The parent owns the sign-in button
// and reads clientId / clientSecret / valid.
ColumnLayout {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  property var guide: null
  property string loadError: ""
  property int step: 0
  property bool showSecret: false
  property string copiedValue: ""

  readonly property color dim: Qt.darker(foreground, 1.6)
  readonly property int stepCount: guide ? guide.steps.length : 0
  // The last stop, after the creation steps, is where the client gets pasted
  readonly property bool atFields: guide !== null && step >= stepCount
  readonly property var current: guide && step < stepCount ? guide.steps[step] : null
  readonly property string clientId: idField.text.trim()
  readonly property string clientSecret: secretField.text.trim()
  readonly property string idError: guide ? fieldError(guide.id, clientId) : ""
  readonly property string secretError: guide ? fieldError(guide.secret, clientSecret) : ""
  readonly property bool valid: guide !== null && clientId !== "" && clientSecret !== ""
                                && idError === "" && secretError === ""
                                && (clientId !== clientSecret)

  spacing: Style.space(8)

  function load(provider, remote) {
    guide = null
    loadError = ""
    step = 0
    idField.text = ""
    secretField.text = ""
    showSecret = false
    if (!service) return
    service.clientGuide(provider, remote, function(g, error) {
      root.guide = g
      root.loadError = g ? "" : (error || "No guide available")
    })
  }

  function clearSecret() { secretField.text = "" }

  function fieldError(spec, value) {
    if (!spec || value === "") return ""
    if (spec.reject && new RegExp(spec.reject).test(value)) return spec.reject_error
    if (!new RegExp(spec.pattern).test(value)) return spec.error
    return ""
  }

  function copy(value) {
    service.copyText(value)
    copiedValue = value
    copiedTimer.restart()
  }

  Timer {
    id: copiedTimer
    interval: 1600
    onTriggered: root.copiedValue = ""
  }

  component Caption: Text {
    Layout.fillWidth: true
    textFormat: Text.PlainText
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    color: root.foreground
    wrapMode: Text.WordWrap
  }

  Caption {
    visible: root.guide === null
    text: root.loadError !== "" ? root.loadError : "Loading the steps…"
    color: root.loadError !== "" ? Color.urgent : root.dim
  }

  // ---- why, and where we are
  Caption {
    visible: root.guide !== null
    text: root.guide ? root.guide.why : ""
    color: root.dim
  }

  RowLayout {
    visible: root.guide !== null
    Layout.fillWidth: true
    spacing: Style.space(4)

    Repeater {
      model: root.stepCount + 1

      Rectangle {
        required property int index
        implicitWidth: index === root.step ? Style.space(18) : Style.space(8)
        implicitHeight: Style.space(8)
        radius: height / 2
        color: index === root.step ? Color.accent
             : index < root.step ? Qt.alpha(Color.accent, 0.45) : Qt.rgba(1, 1, 1, 0.12)
        Behavior on implicitWidth { NumberAnimation { duration: 120 } }

        MouseArea {
          anchors.fill: parent
          anchors.margins: -Style.space(3)
          cursorShape: Qt.PointingHandCursor
          onClicked: root.step = parent.index
        }
      }
    }

    Item { Layout.fillWidth: true }

    Text {
      text: root.atFields ? "Paste your client" : ("Step " + (root.step + 1) + " of " + root.stepCount
                                                    + (root.guide ? " · about " + root.guide.minutes + " min" : ""))
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption - Style.space(2)
      color: root.dim
    }
  }

  // ---- the current step
  BorderSurface {
    visible: root.current !== null
    Layout.fillWidth: true
    implicitHeight: stepCol.implicitHeight + Style.space(20)
    radius: Style.cornerRadius
    color: Qt.rgba(1, 1, 1, 0.03)
    borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

    ColumnLayout {
      id: stepCol
      anchors {
        fill: parent
        margins: Style.space(10)
      }
      spacing: Style.space(8)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Rectangle {
          implicitWidth: Style.space(22)
          implicitHeight: Style.space(22)
          radius: width / 2
          color: Qt.alpha(Color.accent, 0.18)

          Text {
            anchors.centerIn: parent
            text: String(root.step + 1)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            color: Color.accent
          }
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: root.current ? root.current.title : ""
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          color: root.foreground
          wrapMode: Text.WordWrap
        }
      }

      Caption {
        text: root.current ? root.current.text : ""
      }

      Button {
        visible: root.current !== null && root.current.link !== undefined
        iconText: "󰖟"
        text: root.current && root.current.link ? root.current.link.label : ""
        tooltipText: root.current && root.current.link ? root.current.link.url : ""
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        foreground: Color.accent
        bordered: true
        onClicked: root.service.openUrl(root.current.link.url)
      }

      Repeater {
        model: root.current && root.current.copy ? root.current.copy : []

        BorderSurface {
          required property var modelData
          Layout.fillWidth: true
          implicitHeight: copyRow.implicitHeight + Style.space(10)
          radius: Style.cornerRadius
          color: Qt.rgba(0, 0, 0, 0.18)
          borderSpec: Border.none()

          RowLayout {
            id: copyRow
            anchors {
              fill: parent
              leftMargin: Style.space(8)
              rightMargin: Style.space(4)
              topMargin: Style.space(5)
              bottomMargin: Style.space(5)
            }
            spacing: Style.space(6)

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0

              Text {
                text: modelData.label
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption - Style.space(2)
                color: root.dim
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: modelData.value
                font.family: "monospace"
                font.pixelSize: Style.font.caption - Style.space(1)
                color: root.foreground
                wrapMode: Text.WrapAnywhere
              }
            }

            Button {
              Layout.alignment: Qt.AlignVCenter
              iconText: root.copiedValue === modelData.value ? "󰄬" : "󰆏"
              text: root.copiedValue === modelData.value ? "Copied" : "Copy"
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              foreground: root.copiedValue === modelData.value ? Color.accent : root.foreground
              bordered: true
              onClicked: root.copy(modelData.value)
            }
          }
        }
      }
    }
  }

  // ---- the client itself
  ColumnLayout {
    visible: root.atFields
    Layout.fillWidth: true
    spacing: Style.space(4)

    Text {
      text: root.guide ? root.guide.id.label : ""
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      color: root.foreground
    }

    TextField {
      id: idField
      Layout.fillWidth: true
      placeholderText: root.guide ? root.guide.id.placeholder : ""
    }

    Caption {
      visible: root.idError !== ""
      text: root.idError
      color: Color.urgent
      font.pixelSize: Style.font.caption - Style.space(1)
    }

    RowLayout {
      Layout.fillWidth: true
      Layout.topMargin: Style.space(4)

      Text {
        text: root.guide ? root.guide.secret.label : ""
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        color: root.foreground
      }

      Item { Layout.fillWidth: true }

      Button {
        text: root.showSecret ? "Hide" : "Show"
        fontFamily: root.fontFamily
        fontSize: Style.font.caption - Style.space(2)
        foreground: root.dim
        bordered: false
        onClicked: root.showSecret = !root.showSecret
      }
    }

    TextField {
      id: secretField
      Layout.fillWidth: true
      placeholderText: root.guide ? root.guide.secret.placeholder : ""
      password: !root.showSecret
    }

    Caption {
      visible: root.secretError !== "" || (root.clientId !== "" && root.clientId === root.clientSecret)
      text: root.secretError !== "" ? root.secretError : "The client ID and the secret are the same: paste each into its own field"
      color: Color.urgent
      font.pixelSize: Style.font.caption - Style.space(1)
    }

    Caption {
      visible: root.guide !== null && root.guide.signin_note !== ""
      Layout.topMargin: Style.space(4)
      text: root.guide ? "󰋽  " + root.guide.signin_note : ""
      color: root.dim
      font.pixelSize: Style.font.caption - Style.space(1)
    }
  }

  // ---- navigation
  RowLayout {
    visible: root.guide !== null
    Layout.fillWidth: true
    spacing: Style.space(6)

    Button {
      visible: root.step > 0
      iconText: "󰁍"
      text: "Previous"
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      foreground: root.foreground
      bordered: true
      onClicked: root.step = Math.max(0, root.step - 1)
    }

    Button {
      visible: root.step === 0
      text: "I already have one"
      tooltipText: "Skip to the client ID and secret"
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      foreground: root.dim
      bordered: false
      onClicked: root.step = root.stepCount
    }

    Item { Layout.fillWidth: true }

    Button {
      visible: root.atFields
      iconText: "󰈙"
      text: "rclone docs"
      tooltipText: root.guide ? root.guide.docs : ""
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      foreground: root.dim
      bordered: false
      onClicked: root.service.openUrl(root.guide.docs)
    }

    Button {
      visible: !root.atFields
      iconText: "󰁔"
      text: root.step === root.stepCount - 1 ? "Done, paste it" : "Next"
      fontFamily: root.fontFamily
      fontSize: Style.font.caption
      foreground: Color.accent
      bordered: true
      onClicked: root.step = Math.min(root.stepCount, root.step + 1)
    }
  }
}
