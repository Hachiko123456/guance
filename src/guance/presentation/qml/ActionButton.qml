import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    property bool primary: false
    property bool danger: false
    property bool subtle: false
    property string symbol: ""
    implicitHeight: 40
    implicitWidth: contentItem.implicitWidth + 32
    horizontalPadding: 16
    verticalPadding: 9
    font.family: Theme.family
    font.pixelSize: 13
    font.weight: Font.DemiBold
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: text
    opacity: enabled ? 1 : 0.45
    contentItem: Text {
        text: (control.symbol ? control.symbol + "  " : "") + control.text
        font: control.font
        color: control.primary ? Theme.accentText : (control.danger ? Theme.danger : Theme.ink)
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        radius: 9
        color: control.primary ? Theme.accent : control.hovered ? Theme.inset : control.subtle ? "transparent" : Theme.panel
        opacity: control.down ? 0.75 : 1
        border.width: control.visualFocus ? 2 : control.primary || control.subtle ? 0 : 1
        border.color: control.visualFocus ? Theme.accent : Theme.line
    }
}
