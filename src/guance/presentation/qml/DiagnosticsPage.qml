import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

ScrollView {
    id: page
    clip: true
    contentWidth: availableWidth
    property string actionFeedback: ""

    Timer {
        id: feedbackTimer
        interval: 4200
        repeat: false
        onTriggered: page.actionFeedback = ""
    }

    ColumnLayout {
        width: Math.max(0, page.availableWidth)
        spacing: 14

        RowLayout {
            Layout.fillWidth: true
            spacing: 14
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text { text: "诊断"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 26; font.weight: Font.DemiBold }
                Text { text: "诊断信息只用于排查问题，默认已脱敏。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            }
            Badge { text: viewModel.lastError ? "需要处理" : "运行正常"; tone: viewModel.lastError ? "danger" : "success" }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: diagnosticContent.implicitHeight + 28
            radius: 14
            color: viewModel.lastError ? Theme.dangerSoft : Theme.inset
            border.color: viewModel.lastError ? Theme.danger : Theme.line
            ColumnLayout {
                id: diagnosticContent
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Rectangle {
                        implicitWidth: 34; implicitHeight: 34; radius: 17; color: viewModel.lastError ? Theme.danger : Theme.accent
                        Text { anchors.centerIn: parent; text: viewModel.lastError ? "!" : "✓"; color: "white"; font.bold: true; font.pixelSize: 16 }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2
                        Text { text: viewModel.lastError ? "同步失败" : "当前状态"; color: viewModel.lastError ? Theme.danger : Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                        Text { text: viewModel.statusTitle + " · " + viewModel.networkMode; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    SelectableText {
                        objectName: "diagnosticsErrorText"
                        text: viewModel.lastError || "没有待处理错误，系统会在出现异常时把摘要显示在这里。"
                        color: viewModel.lastError ? Theme.danger : Theme.muted
                        font.family: Theme.family
                        font.pixelSize: 12
                        Layout.fillWidth: true
                        maxVisibleHeight: 132
                    }
                }
                RowLayout {
                    objectName: "diagnosticsActionRow"
                    Layout.fillWidth: true
                    spacing: 8
                    Item { Layout.fillWidth: true }
                    ActionButton {
                        objectName: "exportDiagnosticsButton"
                        text: "导出诊断"
                        subtle: true
                        implicitHeight: 34
                        onClicked: {
                            viewModel.request_export_diagnostics()
                            page.actionFeedback = "已提交导出请求，请稍候检查导出目录。"
                            feedbackTimer.restart()
                        }
                    }
                    ToolButton {
                        id: diagnosticsMoreButton
                        objectName: "diagnosticsMoreButton"
                        text: "⋯"
                        implicitWidth: 38
                        implicitHeight: 34
                        font.pixelSize: 18
                        Accessible.name: "更多诊断操作"
                        ToolTip.visible: hovered
                        ToolTip.text: "更多诊断操作"
                        onClicked: diagnosticsMenu.open()
                    }
                    Menu {
                        id: diagnosticsMenu
                        MenuItem {
                            objectName: "openDiagnosticsDirectoryButton"
                            text: "打开导出目录"
                            onTriggered: {
                                viewModel.request_open_diagnostics_directory()
                                page.actionFeedback = "已请求打开导出目录。"
                                feedbackTimer.restart()
                            }
                        }
                        MenuItem {
                            objectName: "openCaptureDirectoryButton"
                            text: "打开报文目录"
                            onTriggered: {
                                viewModel.request_open_capture_directory()
                                page.actionFeedback = "已请求打开当前报文目录。"
                                feedbackTimer.restart()
                            }
                        }
                        MenuSeparator {}
                        MenuItem {
                            objectName: "clearErrorButton"
                            visible: viewModel.lastError !== ""
                            text: "清除提示"
                            onTriggered: {
                                viewModel.clear_last_error()
                                page.actionFeedback = "提示已清除。"
                                feedbackTimer.restart()
                            }
                        }
                    }
                }
                Text {
                    objectName: "diagnosticsActionFeedback"
                    visible: page.actionFeedback !== ""
                    text: page.actionFeedback
                    color: Theme.accent
                    font.family: Theme.family
                    font.pixelSize: 11
                    horizontalAlignment: Text.AlignRight
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }
            }
        }

        ColumnLayout {
            objectName: "diagnosticsList"
            Layout.fillWidth: true
            spacing: 8
            Text { text: "运行详情"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 14; font.bold: true }
            Repeater {
                model: viewModel.diagnostics
                Rectangle {
                    objectName: "diagnosticEntry"
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: detailRow.implicitHeight + 20
                    radius: 10
                    color: Theme.panel
                    border.color: Theme.line
                    RowLayout {
                        id: detailRow
                        anchors.fill: parent
                        anchors.leftMargin: 14
                        anchors.rightMargin: 14
                        anchors.topMargin: 10
                        anchors.bottomMargin: 10
                        spacing: 14
                        Text {
                            text: modelData.label
                            color: Theme.ink
                            font.family: Theme.family
                            font.pixelSize: 12
                            font.bold: true
                            Layout.minimumWidth: 96
                            Layout.preferredWidth: Math.max(112, Math.min(220, page.availableWidth * 0.30))
                            elide: Text.ElideRight
                        }
                        SelectableText {
                            objectName: "diagnosticValueText"
                            text: modelData.value
                            color: Theme.muted
                            font.family: Theme.family
                            font.pixelSize: 11
                            Layout.fillWidth: true
                            maxVisibleHeight: 120
                        }
                    }
                }
            }
        }
        Item { Layout.fillHeight: true; implicitHeight: 8 }
    }
}
