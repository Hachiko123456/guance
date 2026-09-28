import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

ScrollView {
    clip: true
    contentWidth: availableWidth
    ColumnLayout {
        width: availableWidth
        spacing: 18
        Text { text: "诊断"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 26; font.weight: Font.DemiBold }
        Text { text: "诊断信息只用于排查问题，默认已脱敏。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 13 }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 156; radius: 14; color: Theme.inset
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "当前状态"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.statusTitle + " · " + viewModel.networkMode; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12 }
                Text { text: viewModel.lastError || "没有待处理错误"; color: viewModel.lastError ? Theme.danger : Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                ActionButton { objectName: "exportDiagnosticsButton"; text: "导出诊断"; subtle: true; onClicked: viewModel.request_export_diagnostics() }
            }
        }
        Repeater {
            model: viewModel.diagnostics
            Rectangle { required property var modelData; Layout.fillWidth: true; implicitHeight: 54; radius: 10; color: Theme.panel; border.color: Theme.line
                ColumnLayout { anchors.fill: parent; anchors.margins: 12; spacing: 2
                    Text { text: modelData.label; color: Theme.ink; font.family: Theme.family; font.pixelSize: 12; font.bold: true }
                    Text { text: modelData.value; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                }
            }
        }
        Item { Layout.fillHeight: true }
    }
}
