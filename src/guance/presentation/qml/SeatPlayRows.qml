import QtQuick
import QtQuick.Layouts

Item {
    id: rowsRoot
    property var rows: []
    property bool compact: false
    property int cardSpacing: compact ? 1 : 5
    property int rowPadding: compact ? 6 : 10
    readonly property int columns: compact && width >= 480 ? 2 : 1
    implicitWidth: 0
    implicitHeight: rowsGrid.implicitHeight

    GridLayout {
        id: rowsGrid
        objectName: "seatPlayLayout"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        columns: rowsRoot.columns
        columnSpacing: 8
        rowSpacing: compact ? 6 : 8
        uniformCellWidths: true

        Repeater {
            model: rowsRoot.rows
            Rectangle {
                required property var modelData
                objectName: "seatPlayRow-" + modelData.seat
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                implicitHeight: seatRowContent.implicitHeight + rowsRoot.rowPadding * 2
                radius: 6
                color: modelData.active ? Theme.accentSoft : Theme.inset
                border.color: modelData.active ? Theme.accent : Theme.line

                ColumnLayout {
                    id: seatRowContent
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: rowsRoot.rowPadding
                    spacing: compact ? 3 : 5

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Rectangle {
                            objectName: "finishRankBadge-" + modelData.seat
                            visible: (typeof modelData.finishRankLabel === "string")
                                     && modelData.finishRankLabel.length > 0
                                     && (modelData.finishRank === 1
                                         || modelData.finishRank === 2
                                         || modelData.finishRank === 3
                                         || modelData.finishRank === 4)
                            implicitWidth: compact ? 34 : 42
                            implicitHeight: compact ? 20 : 24
                            radius: 6
                            color: modelData.finishRank === 1 ? "#F4D27A" : (modelData.finishRank === 2 ? "#D6DEE8" : (modelData.finishRank === 3 ? "#D9A06D" : Theme.inset))
                            Text {
                                anchors.fill: parent
                                text: modelData.finishRankLabel
                                color: modelData.finishRank === 4 ? Theme.ink : "#17202A"
                                font.family: Theme.family
                                font.pixelSize: compact ? 9 : 11
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                        }
                        Text {
                            text: modelData.arrow || ""
                            visible: typeof modelData.arrow === "string" && modelData.arrow !== ""
                            color: Theme.accent
                            font.family: Theme.family
                            font.pixelSize: compact ? 10 : 12
                            font.bold: true
                        }
                        Text {
                            objectName: "seatLabel-" + modelData.seat
                            text: modelData.name
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: compact ? 10 : 12
                            font.bold: true
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                        }
                        Text {
                            text: modelData.count + " 张" + (modelData.active ? " · 当前" : "")
                            color: modelData.active ? Theme.accent : (modelData.count <= 3 ? Theme.danger : Theme.muted)
                            font.family: Theme.family
                            font.pixelSize: compact ? 10 : 11
                            font.bold: modelData.active
                        }
                    }

                    Flow {
                        objectName: "seatPlayFlow-" + modelData.seat
                        Layout.fillWidth: true
                        width: parent.width
                        spacing: rowsRoot.cardSpacing
                        clip: false
                        Repeater {
                            model: modelData.cards
                            CardFace {
                                required property var modelData
                                cardData: modelData
                                mini: rowsRoot.compact
                                publicPlay: rowsRoot.compact
                            }
                        }
                        Text {
                            visible: modelData.cards.length === 0
                            text: modelData.kind === "pass" ? "过牌" : "未出牌"
                            color: modelData.kind === "pass" ? Theme.amber : Theme.faint
                            font.family: Theme.family
                            font.pixelSize: rowsRoot.compact ? 10 : 11
                            height: rowsRoot.compact ? 24 : 30
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }
    }
}
