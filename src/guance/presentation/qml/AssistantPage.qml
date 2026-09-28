import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

ScrollView {
    id: page
    signal startClicked()
    signal floatingRequested()
    clip: true
    contentWidth: availableWidth
    ColumnLayout {
        width: page.availableWidth
        spacing: 20
        Rectangle {
            Layout.fillWidth: true
            visible: viewModel.demoMode
            implicitHeight: 44
            radius: 9; color: Theme.amberSoft
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 10
                Text { text: "离线演示"; color: Theme.amber; font.family: Theme.family; font.pixelSize: 12; font.bold: true }
                Text { text: "示例数据 · 未连接微信 · 未调用模型"; color: Theme.amber; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true }
                ActionButton { objectName: "exitDemoButton"; text: "退出演示"; subtle: true; implicitHeight: 30; onClicked: viewModel.exit_demo() }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                spacing: 7
                Text { text: "专注每一手，从容看全局。"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 23; font.weight: Font.DemiBold }
                Text { text: viewModel.statusDetail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                Layout.fillWidth: true
            }
            ActionButton {
                objectName: "startButton"
                text: viewModel.demoMode ? "退出演示" : viewModel.busy ? "请稍候…" : viewModel.running ? "停止并恢复网络" : "开始使用"
                primary: !viewModel.running && !viewModel.demoMode
                enabled: !viewModel.busy
                onClicked: {
                    if (viewModel.running || viewModel.demoMode) viewModel.request_stop()
                    else page.startClicked()
                }
            }
        }
        // Main advice surface: intentionally one focal point, no model scores.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: viewModel.adviceVisible ? 322 : 304
            radius: 16
            color: viewModel.adviceVisible ? Theme.hero : Theme.panel
            border.color: viewModel.adviceVisible ? Theme.hero : Theme.line
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 24; spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: viewModel.adviceVisible ? "当前建议" : "对局助手"
                        font.family: Theme.family; font.pixelSize: 13; font.weight: Font.DemiBold
                        color: viewModel.adviceVisible ? "#C0D9D1" : Theme.muted
                        Layout.fillWidth: true
                    }
                    Badge {
                        text: viewModel.demoMode ? "示例推荐" : viewModel.adviceVisible ? "轮到你出牌" : viewModel.statusTitle
                        tone: viewModel.statusTone
                        onDark: viewModel.adviceVisible
                    }
                }
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Column {
                        visible: viewModel.adviceVisible
                        anchors.centerIn: parent
                        spacing: 16
                        width: parent.width
                        Flickable {
                            width: Math.min(parent.width, cardRow.implicitWidth)
                            height: 126
                            anchors.horizontalCenter: parent.horizontalCenter
                            contentWidth: cardRow.implicitWidth
                            clip: true
                            boundsBehavior: Flickable.StopAtBounds
                            Row {
                                id: cardRow; spacing: 9
                                Repeater {
                                    model: viewModel.adviceCandidates.length ? viewModel.adviceCandidates[0].cards : []
                                    CardFace { required property var modelData; cardData: modelData }
                                }
                                Text {
                                    visible: viewModel.adviceCandidates.length && viewModel.adviceCandidates[0].pass
                                    text: "过牌"; color: "#FAFFFC"; font.family: Theme.family; font.pixelSize: 46; font.weight: Font.DemiBold
                                    height: 122; verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: viewModel.adviceCandidates.length ? viewModel.adviceCandidates[0].kind : ""
                            color: "#E6F3EE"; font.family: Theme.family; font.pixelSize: 14
                        }
                    }
                    Column {
                        visible: !viewModel.adviceVisible
                        anchors.centerIn: parent
                        spacing: 13
                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: 56; height: 56; radius: 16; color: Theme.accentSoft
                            BusyIndicator {
                                anchors.centerIn: parent; width: 36; height: 36
                                visible: viewModel.busy || viewModel.adviceState === "thinking"
                                running: visible
                                palette.dark: Theme.accent
                            }
                            Text {
                                anchors.centerIn: parent
                                visible: !viewModel.busy && viewModel.adviceState !== "thinking"
                                text: viewModel.statusTone === "danger" ? "!" : "♠"
                                font.family: "Segoe UI Symbol"; font.pixelSize: 29; color: Theme.accent
                            }
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: viewModel.adviceState === "thinking" ? "正在思考这一手…" : viewModel.statusTone === "danger" ? "先恢复同步，再给建议" : viewModel.observationAvailable ? "等待" + viewModel.turnLabel + "行动" : "你的下一手，在这里"
                            color: Theme.ink; font.family: Theme.family; font.pixelSize: 21; font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: Math.min(420, page.width - 100)
                            text: viewModel.adviceSubtitle
                            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                            color: Theme.muted; font.family: Theme.family; font.pixelSize: 12
                        }
                    }
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: viewModel.adviceVisible ? "#31574E" : Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        text: viewModel.observationAvailable ? "级牌  " + viewModel.level + "     ·     第 " + Math.max(1, viewModel.trick) + " 轮" : "仅使用本人的手牌与公开对局信息"
                        color: viewModel.adviceVisible ? "#B2CCC3" : Theme.muted
                        font.family: Theme.family; font.pixelSize: 12
                        Layout.fillWidth: true
                    }
                    ActionButton {
                        objectName: "floatingButton"
                         text: "打开小窗  ↗"; subtle: true; implicitHeight: 30
                        contentItem: Text { text: parent.text; color: viewModel.adviceVisible ? "#DFF1EB" : Theme.accent; font.family: Theme.family; font.pixelSize: 12; verticalAlignment: Text.AlignVCenter }
                        onClicked: page.floatingRequested()
                    }
                }
            }
        }
        // Other candidates are opt-in, never presented as invented explanations.
        ColumnLayout {
            Layout.fillWidth: true
            visible: viewModel.adviceCandidates.length > 1
            spacing: 10
            ToolButton {
                id: candidatesToggle
                text: checked ? "收起其他候选  ⌃" : "查看其他候选  ⌄"
                checkable: true
                font.family: Theme.family; font.pixelSize: 12
                contentItem: Text { text: candidatesToggle.text; color: Theme.muted; font: candidatesToggle.font }
                background: Item {}
                onClicked: checked = checked
            }
            Flow {
                visible: candidatesToggle.checked
                Layout.fillWidth: true; spacing: 10
                Repeater {
                    model: viewModel.adviceCandidates.slice(1)
                    Rectangle {
                        required property var modelData
                        width: candidateRow.implicitWidth + 26; height: 74; radius: 10; color: Theme.panel; border.color: Theme.line
                        Row {
                            id: candidateRow; spacing: 7; anchors.centerIn: parent
                            Text { text: modelData.kind; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                            Repeater { model: modelData.cards; CardFace { required property var modelData; cardData: modelData; mini: true } }
                        }
                    }
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true
            visible: viewModel.observationAvailable
            implicitHeight: handContent.implicitHeight + 36
            radius: 13; color: Theme.panel; border.color: Theme.line
            ColumnLayout {
                id: handContent
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                anchors.margins: 18
                spacing: 15
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "我的手牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.bold: true; Layout.fillWidth: true }
                    Text { text: viewModel.handCards.length + " 张 · 根据已确认的动作更新"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11 }
                }
                Flow {
                    Layout.fillWidth: true; spacing: 5
                    Repeater { model: viewModel.handCards; CardFace { required property var modelData; cardData: modelData; compact: true } }
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    Repeater {
                        model: viewModel.remainingCards
                        RowLayout {
                            required property var modelData
                            Layout.fillWidth: true; spacing: 7
                            Rectangle { width: 5; height: 5; radius: 3; color: modelData.active ? Theme.accent : Theme.line }
                            Text { text: modelData.name; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12 }
                            Text { text: modelData.count + " 张"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.bold: true }
                            Item { Layout.fillWidth: true }
                        }
                    }
                }
            }
        }
        Rectangle {
            visible: !viewModel.observationAvailable
            Layout.fillWidth: true; implicitHeight: 130; radius: 13; color: Theme.panel; border.color: Theme.line
            RowLayout {
                anchors.fill: parent; anchors.margins: 22; spacing: 24
                Repeater {
                    model: [
                        {number: "01", title: "准备助手", detail: "首次使用，完成明确授权"},
                        {number: "02", title: "打开大掼蛋", detail: "启动后重新打开小游戏"},
                        {number: "03", title: "专注你的牌局", detail: "进入经典玩法，查看建议"}
                    ]
                    ColumnLayout {
                        required property var modelData
                        Layout.fillWidth: true; spacing: 8
                        Text { text: modelData.number; color: Theme.accent; font.family: "Segoe UI"; font.pixelSize: 13; font.weight: Font.DemiBold }
                        Text { text: modelData.title; color: Theme.ink; font.family: Theme.family; font.pixelSize: 14; font.bold: true }
                        Text { text: modelData.detail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
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
