import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Rectangle {
    id: counterRoot
    property var entries: []
    property int unseenTotal: 0
    property bool compact: false
    implicitHeight: counterColumn.implicitHeight + 16
    radius: 0
    color: Theme.panel

    Rectangle { anchors.top: parent.top; width: parent.width; height: 2; color: Theme.accent }

    ColumnLayout {
        id: counterColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: 8
        spacing: 4

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                text: "记牌器"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 13
                font.bold: true
                Layout.fillWidth: true
            }
            Text { text: "公开剩余"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 10 }
            Text {
                objectName: "cardCounterTotal"
                text: String(counterRoot.unseenTotal)
                color: Theme.accent
                font.family: Theme.family
                font.pixelSize: 26
                font.bold: true
            }
        }
        GridLayout {
            id: strip
            objectName: "cardCounterGrid"
            Layout.fillWidth: true
            columns: Math.max(1, Math.min(15, Math.floor(width / 32)))
            uniformCellWidths: true
            columnSpacing: 0
            rowSpacing: 4
            Repeater {
                model: counterRoot.entries
                Rectangle {
                    id: counterCell
                    required property var modelData
                    objectName: "cardCounterCell-" + modelData.rank
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    implicitHeight: 58
                    property int remainingValue: Number(modelData.remaining !== undefined ? modelData.remaining : modelData.unseen)
                    readonly property bool jokerGroup: Number(modelData.rank) >= 14
                    color: jokerGroup ? Theme.amberSoft : "transparent"
                    Rectangle {
                        visible: Number(modelData.rank) === 14
                        width: 1; height: parent.height - 8
                        anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                        color: Theme.amber
                    }
                    Text {
                        objectName: "cardCounterRank-" + modelData.rank
                        anchors.top: parent.top
                        width: parent.width
                        text: modelData.label
                        color: counterCell.remainingValue === 0 ? Theme.faint : Theme.ink
                        font.family: Theme.family
                        font.pixelSize: counterCell.jokerGroup ? 11 : 13
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        objectName: "cardCounterValue-" + modelData.rank
                        y: 17
                        width: parent.width
                        text: String(counterCell.remainingValue)
                        color: counterCell.remainingValue === 0 ? Theme.faint : counterCell.remainingValue <= 2 ? Theme.danger : Theme.ink
                        font.family: Theme.family
                        font.pixelSize: 23
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        objectName: "cardCounterLevel-" + modelData.rank
                        visible: modelData.isLevel
                        anchors.bottom: parent.bottom
                        width: parent.width
                        text: "级"
                        color: Theme.accent
                        font.family: Theme.family
                        font.pixelSize: 10
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Accessible.role: Accessible.StaticText
                    Accessible.name: modelData.label + "：公开剩余 " + remainingValue + (modelData.isLevel ? "，级牌" : "")
                    ToolTip.visible: counterMouse.containsMouse
                    ToolTip.text: modelData.label + "：公开剩余 " + (modelData.remaining !== undefined ? modelData.remaining : modelData.unseen) + "，已出 " + modelData.played
                    ToolTip.delay: 500
                    MouseArea { id: counterMouse; anchors.fill: parent; hoverEnabled: true }
                }
            }
        }
    }
}
