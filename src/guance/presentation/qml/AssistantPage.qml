import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

Item {
    id: page
    objectName: "assistantPage"
    signal startClicked()
    signal floatingRequested()
    signal splitRequested(string layout)

    readonly property bool stackedWindow: viewModel.splitLayout === "stacked"
    readonly property int spacingUnit: Math.max(3, Math.min(8, Math.floor(Math.min(width / 95, height / 95))))
    readonly property bool tight: height < bodyFont.height * 34
    readonly property int panelPadding: tight ? 3 : spacingUnit * 2
    readonly property bool hasHand: (viewModel.displayHandCards || []).length > 0
    readonly property bool settled: viewModel.phase === "ended" || viewModel.phase === "settled"
    readonly property int panelColumns: {
        const unit = bodyFont.height
        if (hasHand && height < unit * 32 && width >= unit * 52)
            return 3
        return width >= unit * 42 ? 2 : 1
    }

    FontMetrics {
        id: bodyFont
        font.family: Theme.family
        font.pixelSize: 12
    }

    ColumnLayout {
        id: content
        objectName: "assistantContent"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: page.tight ? 3 : page.spacingUnit

        RowLayout {
            Layout.fillWidth: true
            spacing: page.spacingUnit
            Text {
                Layout.fillWidth: true
                text: viewModel.demoMode ? "离线演示" : viewModel.statusTitle
                color: viewModel.demoMode ? Theme.amber : Theme.ink
                font.family: Theme.family
                font.pixelSize: page.tight ? 12 : 15
                font.bold: true
                elide: Text.ElideRight
            }
            ActionButton {
                id: splitButton
                objectName: "splitLayoutButton"
                text: page.stackedWindow ? "上下分屏" : "左右分屏"
                subtle: true
                implicitHeight: 27
                implicitWidth: contentItem.implicitWidth + 8
                onClicked: splitMenu.open()
                Menu {
                    id: splitMenu
                    y: splitButton.height
                    MenuItem {
                        objectName: "sideSplitMenuItem"
                        text: "左右分屏"
                        checkable: true
                        checked: !page.stackedWindow
                        onTriggered: page.splitRequested("side")
                    }
                    MenuItem {
                        objectName: "stackedSplitMenuItem"
                        text: "上下分屏"
                        checkable: true
                        checked: page.stackedWindow
                        onTriggered: page.splitRequested("stacked")
                    }
                }
            }
            ActionButton {
                objectName: "floatingButton"
                text: "小窗 ↗"
                subtle: true
                implicitHeight: 27
                implicitWidth: contentItem.implicitWidth + 8
                onClicked: page.floatingRequested()
            }
            ActionButton {
                objectName: "startButton"
                text: viewModel.demoMode ? "退出演示" : viewModel.busy ? "请稍候…" : viewModel.running ? "停止并恢复网络" : "开始使用"
                primary: !viewModel.demoMode
                enabled: !viewModel.busy
                implicitHeight: 27
                implicitWidth: contentItem.implicitWidth + 10
                onClicked: {
                    if (viewModel.running || viewModel.demoMode)
                        viewModel.request_stop()
                    else
                        page.startClicked()
                }
            }
        }

        CardCounter {
            objectName: "mainCardCounter"
            visible: viewModel.running || viewModel.boardAvailable
            Layout.fillWidth: true
            entries: viewModel.cardCounter || []
            unseenTotal: viewModel.counterPublicRemainingTotal || 0
            split: true
            compact: page.tight
            activeTurn: viewModel.myTurn
            roundSummary: viewModel.boardAvailable ?
                "级牌 " + viewModel.displayLevel + " · 第 " + Math.max(1, viewModel.tableTrick) + " 轮 · "
                + (page.settled ? "已结算" : !viewModel.seatConfirmed ? "座位待确认" : viewModel.myTurn ? "轮到你" : viewModel.turnLabel) : ""
        }

        Rectangle {
            objectName: "statusErrorBanner"
            visible: viewModel.statusTone === "danger" && viewModel.lastError !== ""
            Layout.fillWidth: true
            implicitHeight: errorContent.implicitHeight + 8
            radius: 6
            color: Theme.dangerSoft
            border.color: Theme.danger
            RowLayout {
                id: errorContent
                anchors.fill: parent
                anchors.margins: 4
                spacing: 6
                SelectableText {
                    objectName: "statusErrorText"
                    text: viewModel.lastError
                    color: Theme.danger
                    font.family: Theme.family
                    font.pixelSize: 12
                    Layout.fillWidth: true
                    maxVisibleHeight: 55
                }
                ActionButton { objectName: "viewDiagnosticsButton"; text: "查看诊断"; subtle: true; implicitHeight: 28; onClicked: viewModel.navigate("diagnostics") }
            }
        }

        Rectangle {
            objectName: "waitingConnectionGuide"
            visible: !viewModel.boardAvailable
            Layout.fillWidth: true
            implicitHeight: waitingContent.implicitHeight + 12
            radius: 6
            color: Theme.accentSoft
            ColumnLayout {
                id: waitingContent
                anchors.fill: parent
                anchors.margins: 6
                Text { text: viewModel.statusDetail; color: Theme.ink; font.family: Theme.family; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            }
        }

        GridLayout {
            id: play
            visible: viewModel.boardAvailable
            Layout.fillWidth: true
            columns: page.panelColumns
            rowSpacing: page.spacingUnit
            columnSpacing: page.spacingUnit
            uniformCellWidths: true

            Rectangle {
                id: advicePanel
                objectName: "mainAdvicePanel"
                Layout.fillWidth: true
                Layout.fillHeight: page.panelColumns > 1
                Layout.row: 0
                Layout.column: 0
                implicitHeight: adviceContent.implicitHeight + page.panelPadding * 2
                radius: page.tight ? 6 : 10
                color: Theme.panel
                border.color: viewModel.adviceVisible ? Theme.accent : Theme.line

                ColumnLayout {
                    id: adviceContent
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: page.panelPadding
                    spacing: page.tight ? 2 : 7
                    Text { text: "当前建议"; color: Theme.ink; font.family: Theme.family; font.pixelSize: page.tight ? 13 : 16; font.bold: true }
                    Text {
                        objectName: "mainAdviceMessage"
                        visible: !viewModel.adviceVisible
                        Layout.fillWidth: true
                        text: viewModel.phase === "thinking" ? "正在计算建议" :
                              viewModel.adviceSubtitle || viewModel.statusDetail
                        color: Theme.ink
                        font.family: Theme.family
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                    }
                    GridLayout {
                        id: suggestions
                        objectName: "visibleAdviceCandidates"
                        visible: viewModel.adviceVisible
                        Layout.fillWidth: true
                        readonly property int alternatives: Math.max(1, viewModel.adviceCandidates.length - 1)
                        readonly property int capacity: Math.max(1, Math.floor(
                            (width + columnSpacing) / (bodyFont.height * (page.tight ? 7 : 10) + columnSpacing)
                        ))
                        columns: Math.ceil(alternatives / Math.ceil(alternatives / capacity))
                        uniformCellWidths: true
                        rowSpacing: page.tight ? 2 : 5
                        columnSpacing: page.tight ? 3 : 6
                        Repeater {
                            model: viewModel.adviceVisible ? viewModel.adviceCandidates : []
                            Rectangle {
                                id: candidate
                                required property var modelData
                                required property int index
                                objectName: "adviceCandidate-" + index
                                Layout.fillWidth: true
                                Layout.row: index === 0 ? 0 : 1 + Math.floor((index - 1) / suggestions.columns)
                                Layout.column: index === 0 ? 0 : (index - 1) % suggestions.columns
                                Layout.columnSpan: index === 0 ? suggestions.columns : 1
                                Layout.alignment: Qt.AlignTop
                                readonly property int innerPadding: page.tight ? 2 : 6
                                implicitHeight: candidateContent.implicitHeight + innerPadding * 2
                                radius: page.tight ? 4 : 7
                                color: index === 0 ? Theme.accentSoft : Theme.inset
                                border.color: index === 0 ? Theme.accent : Theme.line
                                GridLayout {
                                    id: candidateContent
                                    anchors.top: parent.top
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.margins: candidate.innerPadding
                                    columns: candidate.index === 0 || (page.tight &&
                                        width >= candidateLabel.implicitWidth + columnSpacing
                                                + candidate.modelData.cards.length * 14) ? 2 : 1
                                    rowSpacing: 0
                                    columnSpacing: page.spacingUnit
                                    Text {
                                        id: candidateLabel
                                        Layout.fillWidth: candidateContent.columns === 1
                                        text: (candidate.index === 0 ? "首选 " : "") + candidate.modelData.kind
                                        color: candidate.index === 0 ? Theme.accent : Theme.ink
                                        font.family: Theme.family
                                        font.pixelSize: page.tight ? 11 : candidate.index === 0 ? 14 : 12
                                        font.bold: candidate.index === 0
                                        elide: Text.ElideRight
                                    }
                                    Flow {
                                        id: candidateCards
                                        objectName: candidate.index === 0 ? "currentRecommendationFlow" : "candidateCards-" + candidate.index
                                        visible: candidate.modelData.cards.length > 0
                                        Layout.fillWidth: true
                                        spacing: page.tight ? 1 : 2
                                        readonly property int cardCount: Math.max(1, candidate.modelData.cards.length)
                                        readonly property int desiredWidth: page.tight ? 16 : candidate.index === 0 ? 29 : 22
                                        readonly property int tileWidth: Math.max(13, Math.min(
                                            desiredWidth, Math.floor((width + spacing) / cardCount - spacing)
                                        ))
                                        Repeater {
                                            model: candidate.modelData.cards
                                            CardFace {
                                                required property var modelData
                                                cardData: modelData
                                                displayWidth: candidateCards.tileWidth
                                                displayHeight: Math.round(displayWidth * 1.27)
                                            }
                                        }
                                    }
                                }
                                ToolTip.visible: candidateMouse.containsMouse && modelData.score !== null && modelData.score !== undefined
                                ToolTip.text: modelData.scoreLabel + " " + Number(modelData.score).toFixed(4)
                                MouseArea { id: candidateMouse; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: handPanel
                objectName: "mainHandPanel"
                visible: page.hasHand
                Layout.fillWidth: true
                Layout.fillHeight: page.panelColumns === 3
                Layout.row: page.panelColumns === 3 ? 0 : 1
                Layout.column: page.panelColumns === 3 ? 1 : 0
                Layout.columnSpan: page.panelColumns === 2 ? 2 : 1
                implicitHeight: handContent.implicitHeight + page.panelPadding * 2
                radius: page.tight ? 6 : 10
                color: Theme.panel
                border.color: Theme.line

                ColumnLayout {
                    id: handContent
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: page.panelPadding
                    spacing: page.tight ? 2 : 8
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: viewModel.seatConfirmed ? "我的手牌" : "收到的手牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: page.tight ? 13 : 16; font.bold: true; Layout.fillWidth: true }
                        Text { text: (viewModel.displayHandCards || []).length + " 张"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11 }
                    }
                    Flow {
                        id: hand
                        objectName: "displayHandFlow"
                        Layout.fillWidth: true
                        Layout.maximumWidth: (hand.cardWidth + spacing) * cardsPerRow - spacing
                        Layout.alignment: Qt.AlignHCenter
                        spacing: page.tight ? 2 : 4
                        readonly property int cardCount: Math.max(1, viewModel.displayHandCards.length)
                        readonly property int desiredWidth: page.tight ? 23 : 36
                        readonly property int capacity: Math.max(1, Math.floor(
                            (handContent.width + spacing) / (desiredWidth + spacing)
                        ))
                        readonly property int cardsPerRow: Math.ceil(cardCount / Math.ceil(cardCount / capacity))
                        readonly property int cardWidth: Math.max(14, Math.min(
                            desiredWidth, Math.floor((handContent.width + spacing) / cardsPerRow - spacing)
                        ))
                        Repeater {
                            model: viewModel.displayHandCards || []
                            CardFace {
                                required property var modelData
                                cardData: modelData
                                displayWidth: hand.cardWidth
                                displayHeight: Math.round(displayWidth * (page.tight ? 1.2 : 1.35))
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: publicGameBoard
                objectName: "publicGameBoard"
                Layout.fillWidth: true
                Layout.fillHeight: page.panelColumns > 1
                Layout.row: page.panelColumns > 1 ? 0 : page.hasHand ? 2 : 1
                Layout.column: page.panelColumns - 1
                implicitHeight: boardContent.implicitHeight + page.panelPadding * 2
                radius: page.tight ? 6 : 10
                color: Theme.panel
                border.color: Theme.line

                ColumnLayout {
                    id: boardContent
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: page.panelPadding
                    spacing: page.tight ? 2 : 8
                    Text { text: "四家出牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: page.tight ? 13 : 16; font.bold: true }
                    SeatPlayRows {
                        objectName: "gameOverviewSeats"
                        Layout.fillWidth: true
                        rows: viewModel.displaySeatRows || []
                        compact: true
                        inlineCards: true
                        inlineColumns: width >= bodyFont.height * 24 ? 2 : 1
                        seatTiles: !page.tight
                        rowPadding: page.tight ? 2 : page.spacingUnit
                    }
                }
            }
        }

        RowLayout {
            visible: viewModel.demoMode || (!viewModel.running && !viewModel.boardAvailable)
            Layout.fillWidth: true
            Text { text: viewModel.demoMode ? "示例数据 · 未连接游戏 · 未调用模型" : "本地显示建议 · 由你在游戏中出牌"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
            ActionButton { objectName: "demoButton"; visible: !viewModel.demoMode; text: "先看看界面"; subtle: true; implicitHeight: 27; onClicked: viewModel.enable_demo() }
            ActionButton { objectName: "exitDemoButton"; visible: viewModel.demoMode; text: "退出演示"; subtle: true; implicitHeight: 27; onClicked: viewModel.exit_demo() }
        }
    }
}
