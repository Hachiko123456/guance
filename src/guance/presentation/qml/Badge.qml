import QtQuick
import QtQuick.Layouts

Rectangle {
    id: badge
    property string text: ""
    property string tone: "neutral"
    property bool onDark: false
    implicitHeight: 28
    implicitWidth: label.implicitWidth + 28
    radius: 14
    color: onDark ? "#31534E" : tone === "warning" ? Theme.amberSoft : tone === "danger" ? Theme.dangerSoft : Theme.inset
    Row {
        anchors.centerIn: parent
        spacing: 6
        Rectangle {
            width: 5; height: 5; radius: 3
            anchors.verticalCenter: parent.verticalCenter
            color: badge.onDark ? "#95E2CE" : Theme.toneColor(badge.tone)
        }
        Text {
            id: label
            text: badge.text
            color: badge.onDark ? "#DEEFEA" : Theme.toneColor(badge.tone)
            font.family: Theme.family
            font.pixelSize: 11
        }
    }
}
