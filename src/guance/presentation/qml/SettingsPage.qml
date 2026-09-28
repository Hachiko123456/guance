import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Qt.labs.platform

ScrollView {
    id: page
    objectName: "settingsPage"
    clip: true
    contentWidth: availableWidth

    FileDialog {
        id: modelDialog
        objectName: "modelFileDialog"
        title: "选择 FableDan 模型"
        nameFilters: ["FableDan 模型 (*.npz)", "所有文件 (*)"]
        onAccepted: viewModel.request_model_import(file)
    }

    Popup {
        id: removeDialog
        objectName: "removeCertificateDialog"
        modal: true
        width: Math.min(460, page.width - 40)
        anchors.centerIn: Overlay.overlay
        padding: 22
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.panel; radius: 14; border.color: Theme.line }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: "确定移除掼策的本机网络授权吗？"
                color: Theme.ink
                font.family: Theme.family
                font.pixelSize: 17
                font.bold: true
            }
            Text {
                Layout.fillWidth: true
                text: "移除后，掼策将停止使用本机采集组件，并恢复它接管前的网络设置。下次使用时需要重新授权。"
                color: Theme.muted
                font.family: Theme.family
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                lineHeight: 1.35
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ActionButton { objectName: "cancelRemoveButton"; text: "取消"; subtle: true; onClicked: removeDialog.close() }
                ActionButton {
                    objectName: "confirmRemoveButton"
                    text: "确认移除"
                    danger: true
                    onClicked: {
                        viewModel.request_remove_certificate(true)
                        removeDialog.close()
                    }
                }
            }
        }
    }

    ColumnLayout {
        width: page.availableWidth
        spacing: 18
        Text { text: "设置"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 26; font.weight: Font.DemiBold }
        Text { text: "把复杂设置留在这里，助手页只保留对局需要的信息。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 13 }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 130; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "模型"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.modelSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true }
                ActionButton { objectName: "importModelButton"; text: "导入模型"; subtle: true; onClicked: modelDialog.open() }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 150; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "网络组件"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.certificateSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                RowLayout {
                    ActionButton { objectName: "removeCertificateButton"; text: "移除本机授权"; subtle: true; enabled: !viewModel.running && !viewModel.busy; onClicked: removeDialog.open() }
                    ActionButton { objectName: "repairNetworkButton"; text: "修复网络"; subtle: true; onClicked: viewModel.request_repair() }
                }
            }
        }
        Item { Layout.fillHeight: true }
    }
}
