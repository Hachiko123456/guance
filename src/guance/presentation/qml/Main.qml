import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Window
import "."

ApplicationWindow {
    id: window
    objectName: "guanceWindow"
    visible: true
    width: 1180
    height: 760
    minimumWidth: 960
    minimumHeight: 620
    title: "掼策"
    color: Theme.canvas

    function certificateIsTrusted() {
        return ["trusted", "ready", "installed", "already_trusted"].indexOf(viewModel.certificateState) >= 0
    }

    function startAssistant() {
        if (certificateIsTrusted()) {
            viewModel.request_start()
        } else {
            consent.open()
        }
    }

    function showFloatingWindow() {
        viewModel.set_floating(true)
        floatingWindow.show()
        floatingWindow.raise()
        floatingWindow.requestActivate()
    }

    function closeFloatingWindow() {
        if (floatingWindow.visible) {
            floatingWindow.close()
        }
        if (viewModel.floatingVisible) {
            viewModel.set_floating(false)
        }
    }

    onClosing: closeFloatingWindow()

    header: Rectangle {
        height: 64
        color: Theme.panel
        border.color: Theme.line
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 20
            spacing: 18
            Text { text: "掼策"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 21; font.weight: Font.DemiBold }
            Text { text: "本地牌局智能助手"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true }
            Badge { text: viewModel.modelState === "loaded" || viewModel.modelState === "ready" ? "模型就绪" : "模型待准备"; tone: viewModel.modelState === "loaded" || viewModel.modelState === "ready" ? "success" : "neutral" }
            ActionButton { objectName: "navAssistantButton"; text: "助手"; subtle: viewModel.page !== "assistant"; onClicked: viewModel.navigate("assistant") }
            ActionButton { objectName: "navSettingsButton"; text: "设置"; subtle: viewModel.page !== "settings"; onClicked: viewModel.navigate("settings") }
            ActionButton { objectName: "navDiagnosticsButton"; text: "诊断"; subtle: viewModel.page !== "diagnostics"; onClicked: viewModel.navigate("diagnostics") }
        }
    }

    StackLayout {
        anchors.fill: parent
        anchors.topMargin: 64
        anchors.leftMargin: 28
        anchors.rightMargin: 28
        anchors.bottomMargin: 24
        currentIndex: viewModel.page === "settings" ? 1 : viewModel.page === "diagnostics" ? 2 : 0
        AssistantPage { onStartClicked: window.startAssistant(); onFloatingRequested: window.showFloatingWindow() }
        SettingsPage {}
        DiagnosticsPage {}
    }

    ConsentDialog { id: consent }

    Connections {
        target: viewModel
        function onFloatingVisibleChanged() {
            if (viewModel.floatingVisible) {
                floatingWindow.show()
                floatingWindow.raise()
                floatingWindow.requestActivate()
            } else if (floatingWindow.visible) {
                floatingWindow.hide()
            }
        }
    }

    Window {
        id: floatingWindow
        objectName: "floatingAdviceWindow"
        visible: false
        width: 330
        height: viewModel.adviceVisible ? 188 : 122
        minimumWidth: 280
        minimumHeight: 110
        flags: Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
        title: "掼策 · 当前建议"
        color: "transparent"
        transientParent: window

        onClosing: {
            if (viewModel.floatingVisible) {
                viewModel.set_floating(false)
            }
        }

        Rectangle {
            anchors.fill: parent
            radius: 16
            color: Theme.panel
            border.color: Theme.line
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 10
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "当前建议"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.weight: Font.DemiBold; Layout.fillWidth: true }
                    ActionButton { text: "关闭"; subtle: true; implicitHeight: 28; onClicked: window.closeFloatingWindow() }
                }
                Text { text: viewModel.adviceVisible ? viewModel.adviceSubtitle : viewModel.statusTitle; color: Theme.muted; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Row {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: 6
                    Repeater {
                        model: viewModel.adviceVisible && viewModel.adviceCandidates.length && !viewModel.adviceCandidates[0].pass ? viewModel.adviceCandidates[0].cards : []
                        CardFace { required property var modelData; cardData: modelData; mini: true }
                    }
                    Text {
                        visible: viewModel.adviceVisible && viewModel.adviceCandidates.length && viewModel.adviceCandidates[0].pass
                        text: "过牌"
                        color: Theme.accent
                        font.family: Theme.family
                        font.pixelSize: 18
                        font.weight: Font.DemiBold
                    }
                    Text { visible: !viewModel.adviceVisible; text: "等待局面同步"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 14 }
                }
            }
        }
    }
}
