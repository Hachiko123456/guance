import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

Dialog {
    id: dialog
    objectName: "consentDialog"
    modal: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(560, Math.max(320, parent ? parent.width - 48 : 560))
    height: Math.min(570, Math.max(420, parent ? parent.height - 32 : 570))
    anchors.centerIn: parent
    padding: 26
    onOpened: consent.checked = false
    function accept() { close() }
    function reject() { close() }
    background: Rectangle { color: Theme.panel; radius: 16; border.color: Theme.line }
    contentItem: ColumnLayout {
        spacing: 16
        RowLayout {
            Rectangle {
                width: 44; height: 44; radius: 12; color: Theme.accentSoft
                Text { anchors.centerIn: parent; text: "✓"; color: Theme.accent; font.pixelSize: 23 }
            }
            ColumnLayout {
                spacing: 2
                Text { text: "准备一次，轻松开始"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 21; font.bold: true }
                Text { text: "网络读取授权 · 仅当前 Windows 用户"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12 }
            }
        }
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 16
                Text {
                    Layout.fillWidth: true
                    text: "掼策需要添加一张本机根证书，才能读取大掼蛋的加密对局消息。你不需要寻找文件或输入命令。"
                    wrapMode: Text.WordWrap; lineHeight: 1.4
                    color: Theme.ink; font.family: Theme.family; font.pixelSize: 13
                }
                Rectangle {
                    Layout.fillWidth: true; implicitHeight: details.implicitHeight + 28
                    color: Theme.inset; radius: 10
                    ColumnLayout {
                        id: details
                        anchors.fill: parent; anchors.margins: 14; spacing: 12
                        Repeater {
                            model: [
                                {title: "本机独立证书", detail: "只添加到当前用户的信任库，不写入整台电脑。"},
                                {title: "授权影响", detail: "根证书会让当前用户信任助手签发的证书；系统代理可能影响其他应用联网。"},
                                {title: "随时停止与移除", detail: "停止助手恢复原网络设置；证书默认保留，可在设置中单独移除。"}
                            ]
                            ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true; spacing: 4
                                Text { text: modelData.title; color: Theme.ink; font.family: Theme.family; font.pixelSize: 13; font.bold: true }
                                Text { Layout.fillWidth: true; text: modelData.detail; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; wrapMode: Text.WordWrap; lineHeight: 1.3 }
                            }
                        }
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: "Windows 仍可能显示安全确认，请核对证书名称后由你本人确认。若组织策略禁止安装，助手不会绕过限制。"
                    wrapMode: Text.WordWrap; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; lineHeight: 1.3
                }
            }
        }
        CheckBox {
            id: consent
            objectName: "certificateConsent"
            Layout.fillWidth: true
            text: "我已了解影响，同意添加当前用户根证书"
            font.family: Theme.family; font.pixelSize: 12
            palette.windowText: Theme.ink
            palette.highlight: Theme.accent
            Accessible.name: text
            contentItem: Text { text: consent.text; color: Theme.ink; font: consent.font; wrapMode: Text.WordWrap; leftPadding: 30; verticalAlignment: Text.AlignVCenter }
        }
        RowLayout {
            Layout.fillWidth: true
            Text { text: "拒绝授权不会修改网络。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true }
            ActionButton { objectName: "cancelConsentButton"; text: "暂不授权"; onClicked: dialog.reject() }
            ActionButton {
                objectName: "authorizeButton"
                text: "同意并继续"; primary: true
                enabled: consent.checked && !viewModel.busy && !viewModel.demoMode
                onClicked: { viewModel.request_authorize(consent.checked); dialog.accept() }
            }
        }
    }
}
