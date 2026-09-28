import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
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
    color: Theme.bg

    property bool consentOpen: false

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
            ActionButton { text: "助手"; subtle: viewModel.page !== "assistant"; onClicked: viewModel.navigate("assistant") }
            ActionButton { text: "设置"; subtle: viewModel.page !== "settings"; onClicked: viewModel.navigate("settings") }
            ActionButton { text: "诊断"; subtle: viewModel.page !== "diagnostics"; onClicked: viewModel.navigate("diagnostics") }
        }
    }

    StackLayout {
        anchors.fill: parent
        anchors.topMargin: 64
        anchors.leftMargin: 28
        anchors.rightMargin: 28
        anchors.bottomMargin: 24
        currentIndex: viewModel.page === "settings" ? 1 : viewModel.page === "diagnostics" ? 2 : 0
        AssistantPage { onStartClicked: consent.open() }
        SettingsPage {}
        DiagnosticsPage {}
    }

    ConsentDialog { id: consent }}
