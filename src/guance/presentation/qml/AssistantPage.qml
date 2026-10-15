import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

ScrollView {
    id: page
    signal startClicked()
    signal floatingRequested()
    clip: true
    contentWidth: availableWidth
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ScrollBar.vertical.policy: ScrollBar.AsNeeded

    ColumnLayout {
        id: content
        width: Math.max(0, page.availableWidth)
        spacing: 14

        Rectangle {
            visible: viewModel.demoMode
            Layout.fillWidth: true
            implicitHeight: demoRow.implicitHeight + 20
            radius: 12
            color: Theme.amberSoft
            border.color: Theme.line
            RowLayout {
                id: demoRow
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 12
                spacing: 10
                Text { text: "离线演示"; color: Theme.amber; font.family: Theme.family; font.pixelSize: 12; font.bold: true }
                Text { text: "示例数据 · 未连接游戏 · 未调用模型"; color: Theme.amber; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                ActionButton { objectName: "exitDemoButton"; text: "退出演示"; subtle: true; implicitHeight: 32; onClicked: viewModel.exit_demo() }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 18
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                Text { text: "专注每一手，从容看全局。"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 24; font.weight: Font.DemiBold }
                Text { text: viewModel.statusDetail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            }
            ActionButton {
                objectName: "startButton"
                text: viewModel.demoMode ? "退出演示" : viewModel.busy ? "请稍候…" : viewModel.running ? "停止并恢复网络" : "开始使用"
                primary: !viewModel.running && !viewModel.demoMode
                enabled: !viewModel.busy
                Layout.alignment: Qt.AlignTop
                onClicked: {
                    if (viewModel.running || viewModel.demoMode) viewModel.request_stop()
                    else page.startClicked()
                }
            }
            ActionButton {
                objectName: "floatingButton"
                text: "打开小窗 ↗"
                subtle: true
                Layout.alignment: Qt.AlignTop
                onClicked: page.floatingRequested()
            }
        }

        Rectangle {
            objectName: "statusErrorBanner"
            visible: viewModel.statusTone === "danger" && viewModel.lastError !== ""
            Layout.fillWidth: true
            implicitHeight: errorContent.implicitHeight + 24
            radius: 12
            color: Theme.dangerSoft
            border.color: Theme.danger
            RowLayout {
                id: errorContent
                anchors.fill: parent
                anchors.margins: 12
                spacing: 12
                Rectangle {
                    implicitWidth: 28; implicitHeight: 28; radius: 14; color: Theme.danger
                    Text { anchors.centerIn: parent; text: "!"; color: "white"; font.bold: true; font.pixelSize: 15 }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Text { text: "需要处理"; color: Theme.danger; font.family: Theme.family; font.pixelSize: 12; font.bold: true }
                    SelectableText { objectName: "statusErrorText"; text: viewModel.lastError; color: Theme.danger; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; maxVisibleHeight: 120 }
                }
                ActionButton { objectName: "viewDiagnosticsButton"; text: "查看诊断"; subtle: true; implicitHeight: 32; Layout.alignment: Qt.AlignVCenter; onClicked: viewModel.navigate("diagnostics") }
            }
        }

        Rectangle {
            objectName: "waitingConnectionGuide"
            visible: !viewModel.boardAvailable
            Layout.fillWidth: true
            implicitHeight: waitingContent.implicitHeight + 28
            radius: 14
            color: Theme.accentSoft
            border.color: Theme.line
            ColumnLayout {
                id: waitingContent
                anchors.fill: parent
                anchors.margins: 16
                spacing: 7
                Text { text: viewModel.statusLabel; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.statusDetail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            }
        }

        Rectangle {
            visible: viewModel.boardAvailable
            Layout.fillWidth: true
            implicitHeight: handContent.implicitHeight + 30
            radius: 13
            color: Theme.panel
            border.color: Theme.line
            ColumnLayout {
                id: handContent
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 16; spacing: 9
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: viewModel.seatConfirmed ? "我的手牌" : "收到的手牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.bold: true; Layout.fillWidth: true }
                    Text { text: viewModel.displayHandCards.length + " 张"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11 }
                }
                Flow {
                    objectName: "displayHandFlow"
                    Layout.fillWidth: true
                    width: parent.width
                    spacing: 5
                    Repeater { model: viewModel.displayHandCards; CardFace { required property var modelData; cardData: modelData; compact: true } }
                }
            }
        }

        Rectangle {
            id: advicePanel
            objectName: "mainAdvicePanel"
            visible: viewModel.boardAvailable
            Layout.fillWidth: true
            implicitHeight: adviceContent.implicitHeight + 34
            radius: 16
            color: viewModel.adviceVisible ? Theme.hero : Theme.panel
            border.color: viewModel.adviceVisible ? Theme.hero : Theme.line
            ColumnLayout {
                id: adviceContent
                anchors.fill: parent
                anchors.margins: 18
                spacing: 10
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "当前建议"; color: viewModel.adviceVisible ? "#E6F3EE" : Theme.ink; font.family: Theme.family; font.pixelSize: 16; font.bold: true; Layout.fillWidth: true }
                    Badge { text: viewModel.statusLabel; tone: viewModel.adviceVisible ? "success" : "neutral" }
                }
                Flow {
                    objectName: "currentRecommendationFlow"
                    visible: viewModel.adviceVisible
                    Layout.fillWidth: true
                    width: parent.width
                    spacing: 7
                    Repeater { model: viewModel.adviceVisible && viewModel.adviceCandidates.length && !viewModel.adviceCandidates[0].pass ? viewModel.adviceCandidates[0].cards : []
                        CardFace { required property var modelData; cardData: modelData }
                    }
                    Text { visible: viewModel.adviceVisible && viewModel.adviceCandidates.length && viewModel.adviceCandidates[0].pass; text: "过牌"; color: "#F3FFFB"; font.family: Theme.family; font.pixelSize: 30; font.bold: true; height: 122; verticalAlignment: Text.AlignVCenter }
                }
                Text {
                    visible: !viewModel.adviceVisible
                    text: viewModel.tableAvailable && !viewModel.seatConfirmed ? "待确认，暂不推荐" : viewModel.tableCurrentSeat >= 0 && !viewModel.myTurn ? "等待其他玩家" : viewModel.adviceState === "thinking" ? "等待建议" : "等待建议"
                    color: Theme.muted; font.family: Theme.family; font.pixelSize: 14; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
                }
                Text { visible: viewModel.adviceVisible; text: viewModel.adviceCandidates.length ? viewModel.adviceCandidates[0].kind : ""; color: "#E6F3EE"; font.family: Theme.family; font.pixelSize: 13; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: viewModel.adviceVisible ? "#31574E" : Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: viewModel.boardAvailable ? (viewModel.seatConfirmed ? viewModel.seatOrderLabel : "绝对座位顺序 · 待确认") : "仅使用本人的手牌与公开对局信息"; color: viewModel.adviceVisible ? "#B2CCC3" : Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            visible: viewModel.adviceCandidates.length > 1 && viewModel.adviceVisible
            spacing: 8
            ToolButton {
                id: candidatesToggle
                text: checked ? "收起其他候选  ⌃" : "查看其他候选  ⌄"
                checkable: true
                font.family: Theme.family; font.pixelSize: 12
                contentItem: Text { text: candidatesToggle.text; color: Theme.muted; font: candidatesToggle.font }
                background: Item {}
            }
            Flow {
                visible: candidatesToggle.checked
                Layout.fillWidth: true
                width: parent.width
                spacing: 8
                Repeater {
                    model: viewModel.adviceCandidates.slice(1)
                    Rectangle {
                        required property var modelData
                        width: Math.min(360, parent.width)
                        implicitHeight: candidateFlow.implicitHeight + 18
                        radius: 10; color: Theme.panel; border.color: Theme.line
                        Flow { id: candidateFlow; anchors.fill: parent; anchors.margins: 9; spacing: 5
                            Text { text: modelData.kind; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12 }
                            Repeater { model: modelData.cards; CardFace { required property var modelData; cardData: modelData; mini: true } }
                            Text { visible: modelData.score !== null && modelData.score !== undefined; text: modelData.scoreLabel + "：" + Number(modelData.score).toFixed(4); color: Theme.faint; font.family: Theme.family; font.pixelSize: 10 }
                        }
                    }
                }
            }
        }

        Rectangle {
            id: publicGameBoard
            objectName: "publicGameBoard"
            visible: viewModel.boardAvailable
            Layout.fillWidth: true
            implicitHeight: boardContent.implicitHeight + 32
            radius: 14
            color: Theme.panel
            border.color: Theme.line
            ColumnLayout {
                id: boardContent
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "四家出牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 16; font.bold: true; Layout.fillWidth: true }
                    Text { text: viewModel.tableCurrentSeat >= 0 ? (viewModel.myTurn ? "轮到你" : "轮到" + viewModel.turnLabel) : "待确认"; color: viewModel.myTurn ? Theme.accent : Theme.muted; font.family: Theme.family; font.pixelSize: 11; font.bold: viewModel.myTurn }
                }
                Text { text: viewModel.seatConfirmed ? viewModel.seatOrderLabel : "绝对座位 0 → 1 → 2 → 3"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                GridLayout {
                    objectName: "mainGameMetrics"
                    Layout.fillWidth: true
                    columns: width < 330 ? 1 : 3
                    columnSpacing: 6
                    rowSpacing: 6
                    Repeater {
                        model: [
                            {label: "级牌", value: String(viewModel.displayLevel), tone: "accent"},
                            {label: "轮次", value: "第 " + Math.max(1, viewModel.tableTrick) + " 轮", tone: "normal"},
                            {label: "当前行动", value: viewModel.tableCurrentSeat >= 0 ? (viewModel.myTurn ? "轮到你" : "轮到" + viewModel.turnLabel) : "待确认", tone: viewModel.myTurn ? "accent" : "normal"}
                        ]
                        Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            implicitHeight: 48
                            radius: 10
                            color: modelData.tone === "accent" ? Theme.accentSoft : Theme.inset
                            border.color: modelData.tone === "accent" ? Theme.accent : Theme.line
                            ColumnLayout { anchors.fill: parent; anchors.margins: 7; spacing: 1
                                Text { text: modelData.label; color: Theme.muted; font.family: Theme.family; font.pixelSize: 10; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
                                Text { text: modelData.value; color: modelData.tone === "accent" ? Theme.accent : Theme.ink; font.family: Theme.family; font.pixelSize: 12; font.bold: true; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight }
                            }
                        }
                    }
                }
                SeatPlayRows { id: mainSeatRows; objectName: "gameOverviewSeats"; Layout.fillWidth: true; rows: viewModel.displaySeatRows; compact: false }
            }
        }

        CardCounter {
            visible: viewModel.tableAvailable
            objectName: "mainCardCounter"
            Layout.fillWidth: true
            entries: viewModel.cardCounter
            unseenTotal: viewModel.counterPublicRemainingTotal
        }

        Rectangle {
            visible: !viewModel.boardAvailable && ["idle", "stopped", "ended"].indexOf(viewModel.phase) >= 0
            Layout.fillWidth: true
            implicitHeight: onboardingContent.implicitHeight + 28
            radius: 13; color: Theme.panel; border.color: Theme.line
            ColumnLayout {
                id: onboardingContent
                anchors.fill: parent; anchors.margins: 18; spacing: 10
                Text { text: "开始前确认"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 14; font.bold: true }
                GridLayout {
                    Layout.fillWidth: true; columns: page.availableWidth < 760 ? 1 : 3; columnSpacing: 20
                    Repeater {
                        model: [
                            {number: "01", title: "准备助手", detail: "完成授权并启动采集"},
                            {number: "02", title: "进入牌桌", detail: "等待牌局数据到达"},
                            {number: "03", title: "查看建议", detail: "确认座位后再推荐"}
                        ]
                        ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true; spacing: 4
                            Text { text: modelData.number; color: Theme.accent; font.family: "Segoe UI"; font.pixelSize: 12; font.bold: true }
                            Text { text: modelData.title; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.bold: true }
                            Text { text: modelData.detail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Text { text: "本地运行 · 不自动出牌"; color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true }
            ActionButton { objectName: "demoButton"; visible: !viewModel.running && !viewModel.demoMode; text: "先看看界面"; subtle: true; implicitHeight: 30; onClicked: viewModel.enable_demo() }
        }
        Item { implicitHeight: 6 }
    }
}
