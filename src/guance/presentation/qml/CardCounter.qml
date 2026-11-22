import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Rectangle {
    id: counterRoot
    property var entries: []
    property int unseenTotal: 0
    property bool compact: false
    property bool split: false
    property string roundSummary: ""
    property bool activeTurn: false
    readonly property var displayEntries: split && entries.length === 0 ?
        ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "小王", "大王"].map(
            (label, index) => ({rank: index + 1, label: label, remaining: -1, isLevel: false})
        ) : entries
    readonly property int panelPadding: split ? compact ? 5 : 12 : 8
    implicitHeight: counterColumn.implicitHeight + panelPadding * 2
    radius: split ? 9 : 0
    color: Theme.panel
    border.width: split ? 1 : 0
    border.color: Theme.line

    FontMetrics {
        id: rankFont
        font.family: Theme.family
        font.pixelSize: 13
    }

    ColumnLayout {
        id: counterColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: counterRoot.panelPadding
        anchors.leftMargin: counterRoot.split ? counterRoot.panelPadding : 0
        anchors.rightMargin: counterRoot.split ? counterRoot.panelPadding : 0
        spacing: counterRoot.split && !counterRoot.compact ? 7 : 2

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                text: "记牌器"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: counterRoot.split && !counterRoot.compact ? 16 : 13
                font.bold: true
            }
            Text {
                objectName: "mainGameMetrics"
                visible: counterRoot.split && counterRoot.roundSummary !== ""
                Layout.fillWidth: true
                text: counterRoot.roundSummary
                color: counterRoot.activeTurn ? Theme.accent : Theme.ink
                font.family: Theme.family
                font.pixelSize: 11
                font.bold: counterRoot.activeTurn
                elide: Text.ElideRight
            }
            Item { visible: !counterRoot.split || counterRoot.roundSummary === ""; Layout.fillWidth: true }
            Text { text: "公开剩余"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11 }
            Text {
                objectName: "cardCounterTotal"
                text: counterRoot.entries.length === 0 && counterRoot.split ? "—" : String(counterRoot.unseenTotal)
                color: Theme.accent
                font.family: Theme.family
                font.pixelSize: counterRoot.split ? 15 : 26
                font.bold: true
            }
        }
        Item {
            id: strip
            objectName: "cardCounterGrid"
            Layout.fillWidth: true
            readonly property int itemCount: counterRoot.displayEntries.length
            readonly property int tileGap: counterRoot.split ? (counterRoot.compact ? 3 : 5) : 2
            readonly property int minTileWidth: counterRoot.split ?
                Math.ceil(Math.max(28, rankFont.advanceWidth("小王") + (counterRoot.compact ? 8 : 14))) : 32
            readonly property int capacity: Math.max(1, Math.floor((width + tileGap) / (minTileWidth + tileGap)))
            readonly property int rowCount: Math.ceil(itemCount / capacity)
            readonly property int columns: Math.ceil(itemCount / Math.max(1, rowCount))
            readonly property int tileWidth: Math.floor((width + tileGap) / Math.max(1, columns) - tileGap)
            readonly property int tileHeight: counterRoot.split ? (counterRoot.compact ? 31 : 49) : 58
            implicitHeight: Math.max(0, rowCount * (tileHeight + tileGap) - tileGap)

            Repeater {
                model: counterRoot.displayEntries
                Rectangle {
                    id: counterCell
                    required property int index
                    required property var modelData
                    objectName: "cardCounterCell-" + modelData.rank
                    readonly property int rowNumber: Math.floor(index / Math.max(1, strip.columns))
                    readonly property int tilesInRow: Math.min(strip.columns, strip.itemCount - rowNumber * strip.columns)
                    readonly property int rowWidth: tilesInRow * (strip.tileWidth + strip.tileGap) - strip.tileGap
                    x: Math.floor((strip.width - rowWidth) / 2) + (index % strip.columns) * (strip.tileWidth + strip.tileGap)
                    y: rowNumber * (strip.tileHeight + strip.tileGap)
                    width: Math.max(0, strip.tileWidth)
                    height: strip.tileHeight
                    property int remainingValue: Number(modelData.remaining !== undefined ? modelData.remaining : modelData.unseen)
                    readonly property bool jokerGroup: Number(modelData.rank) >= 14
                    readonly property int quantityTier: remainingValue <= 0 ? 0 : remainingValue <= 2 ? 1 : remainingValue <= 4 ? 2 : 3
                    color: !counterRoot.split ? (jokerGroup ? Theme.amberSoft : "transparent") :
                           quantityTier === 0 ? Theme.inset : quantityTier === 1 ? Theme.dangerSoft :
                           quantityTier === 2 ? Theme.amberSoft : Theme.accentSoft
                    radius: counterRoot.split ? 5 : 0
                    border.width: counterRoot.split && modelData.isLevel ? 1 : 0
                    border.color: Theme.accent
                    Rectangle {
                        visible: !counterRoot.split && Number(modelData.rank) === 14
                        width: 1; height: parent.height - 8
                        anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                        color: Theme.amber
                    }
                    Text {
                        objectName: "cardCounterRank-" + modelData.rank
                        anchors.top: parent.top
                        anchors.topMargin: counterRoot.split && !counterRoot.compact ? 4 : 0
                        width: parent.width
                        text: modelData.label
                        color: counterRoot.split ? Theme.ink : counterCell.remainingValue === 0 ? Theme.faint : Theme.ink
                        font.family: Theme.family
                        font.pixelSize: counterRoot.split ? (counterCell.jokerGroup ? 11 : counterRoot.compact ? 13 : 16) : counterCell.jokerGroup ? 11 : 13
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        objectName: "cardCounterValue-" + modelData.rank
                        anchors.bottom: counterRoot.split ? parent.bottom : undefined
                        anchors.bottomMargin: counterRoot.split && !counterRoot.compact ? 3 : 0
                        y: counterRoot.split ? 0 : 17
                        width: parent.width
                        text: counterCell.remainingValue < 0 ? "—" : String(counterCell.remainingValue)
                        color: !counterRoot.split ? (counterCell.remainingValue === 0 ? Theme.faint : counterCell.remainingValue <= 2 ? Theme.danger : Theme.ink) :
                               counterCell.quantityTier === 0 ? Theme.muted : counterCell.quantityTier === 1 ? Theme.danger :
                               counterCell.quantityTier === 2 ? Theme.amber : Theme.accent
                        font.family: Theme.family
                        font.pixelSize: counterRoot.split ? (counterRoot.compact ? 16 : 22) : 23
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        objectName: "cardCounterLevel-" + modelData.rank
                        visible: !counterRoot.split && modelData.isLevel
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
                    Accessible.name: modelData.label + "，" + (remainingValue < 0 ? "等待同步" : "公开剩余 " + remainingValue) + (modelData.isLevel ? "，级牌" : "")
                    ToolTip.visible: counterMouse.containsMouse && remainingValue >= 0
                    ToolTip.text: modelData.label + "：公开剩余 " + (modelData.remaining !== undefined ? modelData.remaining : modelData.unseen) + "，已出 " + modelData.played
                    ToolTip.delay: 500
                    MouseArea { id: counterMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                }
            }
        }
    }
}
