import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

ScrollView {
    clip: true
    contentWidth: availableWidth
    ColumnLayout {
        width: availableWidth
        spacing: 18
        Text { text: "设置"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 26; font.weight: Font.DemiBold }
        Text { text: "把复杂设置留在这里，助手页只保留对局需要的信息。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 13 }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 130; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "模型"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.modelSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true }
                ActionButton { text: "导入模型"; subtle: true; onClicked: viewModel.request_model_import("") }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 150; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "网络组件"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.certificateSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                RowLayout {
                    ActionButton { text: "移除本机授权"; subtle: true; onClicked: viewModel.request_remove_certificate(true) }
                    ActionButton { text: "修复网络"; subtle: true; onClicked: viewModel.request_repair() }
                }
            }
        }
        Item { Layout.fillHeight: true }
    }
}
