import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Window
import Guance.Presentation 1.0
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
    property bool mainCloseRequested: false
    property bool floatingCloseInProgress: false

    Binding { target: Theme; property: "dark"; value: viewModel.themeMode === "dark" || (viewModel.themeMode === "system" && Qt.styleHints.colorScheme === Qt.Dark) }

    function certificateIsTrusted() {
        var state = String(viewModel.certificateState || "").toLowerCase()
        return ["trusted", "ready", "installed", "already_trusted"].indexOf(state) >= 0
    }

    function startAssistant() {
        if (certificateIsTrusted()) {
            viewModel.request_start()
        } else {
            consent.open()
            consent.forceActiveFocus()
        }
    }

    function showFloatingWindow() {
        floatingCloseInProgress = false
        window.hide()
        viewModel.set_floating(true)
    }

    function closeFloatingWindow() {
        // Flush manual geometry once, then cancel all delayed work before hiding.
        if (floatingCloseInProgress) {
            floatingWindow.hide()
            return
        }
        floatingCloseInProgress = true
        floatingWindow.stopLayoutSave()
        dockMenu.close()
        if (floatingWindow.visible)
            floatingWindow.hide()
        if (viewModel.floatingVisible)
            viewModel.set_floating(false)
        if (!mainCloseRequested) {
            window.show()
            window.raise()
            window.requestActivate()
        }
        floatingCloseInProgress = false
    }

    function exitApplication() {
        mainCloseRequested = true
        closeFloatingWindow()
        Qt.quit()
    }

    onClosing: {
        mainCloseRequested = true
        floatingWindow.stopLayoutSave()
        if (floatingWindow.visible)
            floatingWindow.hide()
        if (viewModel.floatingVisible)
            viewModel.set_floating(false)
    }

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
            Text { text: "本地牌局智能助手"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
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
                if (window.floatingCloseInProgress)
                    return
                window.hide()
                floatingWindow.show()
                floatingWindow.applySavedLayout(viewModel.floatingLayout)
                floatingWindow.raise()
                floatingWindow.requestActivate()
            } else {
                window.closeFloatingWindow()
            }
        }
    }

    Connections {
        target: viewModel
        function onFloatingLayoutChanged() {
            if (floatingWindow.visible && !window.floatingCloseInProgress && !floatingWindow.restoringLayout && !floatingWindow.savingLayout)
                floatingWindow.applySavedLayout(viewModel.floatingLayout)
        }
        function onScreenWorkAreaChanged() { floatingWindow.requestFit(false) }
    }

    FloatingWindow {
        id: floatingWindow
        objectName: "floatingAdviceWindow"
        visible: false
        width: 560
        height: 460
        minimumWidth: 320
        minimumHeight: 240
        flags: Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
        title: "掼策 · 当前建议"
        color: "transparent"
        transientParent: null
        property string dockMode: "right"
        property bool applyingGeometry: false
        property bool restoringLayout: false
        property bool savingLayout: false
        property bool layoutDirty: false
        property bool allowFitShrink: false
        // Native window changes can arrive one event after a QML assignment.
        // Remember the last programmatic geometry so those events cannot turn
        // fit mode into manual mode; a real user resize still differs.
        property int lastProgrammaticWidth: -1
        property int lastProgrammaticHeight: -1
        property int lastProgrammaticX: -2147483648
        property int lastProgrammaticY: -2147483648
        readonly property int contentFitHeight: Math.ceil(floatingContent.implicitHeight + floatingHeader.height + (floatingCounter.visible ? floatingCounter.implicitHeight + 8 : 0) + 32)

        Timer {
            id: layoutSaveTimer
            objectName: "floatingLayoutSaveTimer"
            interval: 120
            repeat: false
            onTriggered: floatingWindow.saveLayout()
        }

        Timer {
            id: fitTimer
            objectName: "floatingFitTimer"
            interval: 40
            onTriggered: floatingWindow.fitContent()
        }

        function workArea() {
            var area = viewModel.screen_work_area(floatingWindow)
            if (!area || Number(area.width) <= 0 || Number(area.height) <= 0)
                return {x: Number(window.x || 0), y: Number(window.y || 0), width: Math.max(minimumWidth, Number(window.width || 560)), height: Math.max(minimumHeight, Number(window.height || 460))}
            return {x: Number(area.x || 0), y: Number(area.y || 0), width: Number(area.width), height: Number(area.height)}
        }

        function recordProgrammaticGeometry() {
            lastProgrammaticWidth = Math.round(width)
            lastProgrammaticHeight = Math.round(height)
            lastProgrammaticX = Math.round(x)
            lastProgrammaticY = Math.round(y)
        }

        function layoutSnapshot() {
            return {saved: true, mode: dockMode, sizeMode: sizeMode, x: Math.round(x), y: Math.round(y), width: Math.round(nativeWidth), height: Math.round(nativeHeight)}
        }

        function scheduleLayoutSave() {
            if (visible && !applyingGeometry && !restoringLayout && !window.floatingCloseInProgress) {
                layoutDirty = true
                layoutSaveTimer.restart()
            }
        }

        function saveLayout() {
            if (!layoutDirty) return
            savingLayout = true
            layoutDirty = false
            viewModel.save_floating_layout(layoutSnapshot())
            savingLayout = false
        }

        function stopLayoutSave() {
            layoutSaveTimer.stop()
            fitTimer.stop()
            allowFitShrink = false
            saveLayout()
        }

        function requestFit(shrink) {
            if (!visible || window.floatingCloseInProgress) return
            allowFitShrink = allowFitShrink || shrink
            fitTimer.restart()
        }

        function fitContent() {
            if (!visible || window.floatingCloseInProgress) return
            clampToWorkArea()
            if (sizeMode === "fit") {
                var area = workArea()
                var desired = Math.max(minimumHeight, contentFitHeight)
                applyingGeometry = true
                height = Math.min(area.height, allowFitShrink ? desired : Math.max(height, desired))
                positionDock(area)
                applyingGeometry = false
                clampToWorkArea()
            }
            allowFitShrink = false
            recordProgrammaticGeometry()
            scheduleLayoutSave()
        }

        function applySavedLayout(layout) {
            restoringLayout = true
            applyingGeometry = true
            layout = layout || {}
            minimumWidth = 1
            minimumHeight = 1
            dockMode = ["right", "left", "top", "bottom", "free"].indexOf(String(layout.mode)) >= 0 ? String(layout.mode) : "right"
            sizeMode = layout.sizeMode === "fit" || !layout.saved ? "fit" : "manual"
            width = Number(layout.saved ? layout.width : 560)
            height = Number(layout.saved ? layout.height : 240)
            x = Number(layout.saved ? layout.x : window.x)
            y = Number(layout.saved ? layout.y : window.y)
            if (!layout.saved) positionDock(workArea())
            applyingGeometry = false
            clampToWorkArea()
            restoringLayout = false
            recordProgrammaticGeometry()
            requestFit(!layout.saved)
        }

        function clampToWorkArea() {
            var area = workArea()
            var areaWidth = Math.max(1, Number(area.width))
            var areaHeight = Math.max(1, Number(area.height))
            var wasApplying = applyingGeometry
            applyingGeometry = true
            minimumWidth = Math.min(400, areaWidth)
            minimumHeight = Math.min(Math.max(200, floatingHeader.height + (floatingCounter.visible ? floatingCounter.implicitHeight : 0) + 40), areaHeight)
            width = Math.min(Math.max(width, minimumWidth), areaWidth)
            height = Math.min(Math.max(height, minimumHeight), areaHeight)
            x = Math.max(area.x, Math.min(x, area.x + Math.max(0, areaWidth - width)))
            y = Math.max(area.y, Math.min(y, area.y + Math.max(0, areaHeight - height)))
            applyingGeometry = wasApplying
        }

        function positionDock(area) {
            if (dockMode === "right" || dockMode === "left") {
                y = area.y
                x = dockMode === "right" ? area.x + area.width - width : area.x
            } else if (dockMode === "top" || dockMode === "bottom") {
                x = area.x + Math.floor((area.width - width) / 2)
                y = dockMode === "bottom" ? area.y + area.height - height : area.y
            }
        }

        function dock(mode, save) {
            dockMode = mode
            clampToWorkArea()
            applyingGeometry = true
            positionDock(workArea())
            applyingGeometry = false
            clampToWorkArea()
            recordProgrammaticGeometry()
            if (save !== false) scheduleLayoutSave()
        }

        function resetDock() {
            dockMode = "right"
            autoFit()
        }

        function autoFit() {
            applyingGeometry = true
            sizeMode = "fit"
            width = 560
            applyingGeometry = false
            clampToWorkArea()
            recordProgrammaticGeometry()
            requestFit(true)
        }

        function manualResize() {
            if (applyingGeometry || restoringLayout || !visible) return
            if (Math.round(width) === lastProgrammaticWidth &&
                    Math.round(height) === lastProgrammaticHeight) return
            sizeMode = "manual"
            allowFitShrink = false
            clampToWorkArea()
            recordProgrammaticGeometry()
            scheduleLayoutSave()
        }

        function moved() {
            if (applyingGeometry || restoringLayout || !visible) return
            if (Math.round(x) === lastProgrammaticX &&
                    Math.round(y) === lastProgrammaticY) return
            dockMode = "free"
            scheduleLayoutSave()
            requestFit(false)
        }

        onWidthChanged: manualResize()
        onHeightChanged: manualResize()
        onXChanged: moved()
        onYChanged: moved()
        onScreenChanged: requestFit(false)
        onContentFitHeightChanged: requestFit(false)
        Component.onCompleted: applySavedLayout(viewModel.floatingLayout)
        onCloseRequested: window.closeFloatingWindow()

        Rectangle {
            anchors.fill: parent
            radius: 0
            color: Theme.panel
            border.color: Theme.line

                    RowLayout {
                        id: floatingHeader
                        objectName: "floatingHeader"
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.margins: 12
                        height: 32
                        spacing: 6
                        Text { text: "对局助手"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 16; font.bold: true; Layout.fillWidth: true; elide: Text.ElideRight }
                        Badge { objectName: "floatingStatusBadge"; text: viewModel.statusLabel; tone: viewModel.statusTone === "danger" ? "danger" : viewModel.myTurn ? "success" : "info" }
                        ToolButton {
                            objectName: "floatingMenuButton"
                            text: "⋯"
                            implicitWidth: 32
                            implicitHeight: 30
                            font.pixelSize: 19
                            onClicked: dockMenu.open()
                            ToolTip.visible: hovered
                            ToolTip.text: "窗口布局"
                        }
                        ToolButton {
                            id: floatingCloseButton
                            objectName: "floatingCloseButton"
                            text: "×"
                            Layout.preferredWidth: 32
                            Layout.preferredHeight: 30
                            font.pixelSize: 22
                            Accessible.name: "关闭悬浮窗"
                            contentItem: Text { text: floatingCloseButton.text; font: floatingCloseButton.font; color: Theme.ink; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { radius: 4; color: floatingCloseButton.hovered ? Theme.dangerSoft : "transparent" }
                            onClicked: window.closeFloatingWindow()
                            ToolTip.visible: hovered
                            ToolTip.text: "关闭悬浮窗"
                        }
                        Menu {
                            id: dockMenu
                            MenuItem { objectName: "dockRightMenuItem"; text: "停靠右侧"; onTriggered: floatingWindow.dock("right") }
                            MenuItem { objectName: "dockLeftMenuItem"; text: "停靠左侧"; onTriggered: floatingWindow.dock("left") }
                            MenuItem { objectName: "dockTopMenuItem"; text: "停靠顶部"; onTriggered: floatingWindow.dock("top") }
                            MenuItem { objectName: "dockBottomMenuItem"; text: "停靠底部"; onTriggered: floatingWindow.dock("bottom") }
                            MenuItem { objectName: "dockFreeMenuItem"; text: "自由浮动"; onTriggered: floatingWindow.dock("free") }
                            MenuSeparator {}
                            MenuItem { objectName: "floatingAutoFitMenuItem"; text: "适应内容"; onTriggered: floatingWindow.autoFit() }
                            MenuItem { objectName: "dockResetMenuItem"; text: "恢复右侧默认"; onTriggered: floatingWindow.resetDock() }
                            Menu {
                                id: confirmSeatMenu
                                objectName: "confirmSeatMenu"
                                title: "确认首家"
                                visible: viewModel.tableAvailable && !viewModel.seatConfirmed
                                enabled: visible
                                MenuItem { objectName: "confirmSeatSelfMenuItem"; text: "首家是我"; onTriggered: viewModel.request_confirm_seat("self") }
                                MenuItem { objectName: "confirmSeatLeftMenuItem"; text: "首家是左家"; onTriggered: viewModel.request_confirm_seat("left") }
                                MenuItem { objectName: "confirmSeatPartnerMenuItem"; text: "首家是对家"; onTriggered: viewModel.request_confirm_seat("partner") }
                                MenuItem { objectName: "confirmSeatRightMenuItem"; text: "首家是右家"; onTriggered: viewModel.request_confirm_seat("right") }
                            }
                            MenuSeparator {}
                            MenuItem { objectName: "exitApplicationMenuItem"; text: "退出应用"; onTriggered: window.exitApplication() }
                        }
                    }

            ScrollView {
                id: floatingScroll
                objectName: "floatingMiddleScroll"
                anchors.top: floatingHeader.bottom
                anchors.bottom: floatingCounter.visible ? floatingCounter.top : parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                anchors.topMargin: 8
                anchors.bottomMargin: 8
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: ScrollBar.AsNeeded

                ColumnLayout {
                    id: floatingContent
                    objectName: "floatingMiddleContent"
                    // Reserve the scrollbar gutter even when it is hidden.
                    width: Math.max(0, floatingScroll.availableWidth - 12)
                    spacing: 8

                    GridLayout {
                        objectName: "floatingGameMetrics"
                        visible: viewModel.boardAvailable
                        Layout.fillWidth: true
                        columns: width < 300 ? 1 : 3
                        columnSpacing: 5
                        rowSpacing: 5
                        Repeater {
                            model: [
                                {label: "级牌", value: String(viewModel.displayLevel), tone: "accent"},
                                {label: "轮次", value: "第 " + Math.max(1, viewModel.tableTrick) + " 轮", tone: "normal"},
                                {label: "当前行动", value: viewModel.tableCurrentSeat >= 0 ? (viewModel.myTurn ? "轮到你" : "轮到" + viewModel.turnLabel) : "待确认", tone: viewModel.myTurn ? "accent" : "normal"}
                            ]
                            Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                implicitHeight: 42
                                radius: 8
                                color: modelData.tone === "accent" ? Theme.accentSoft : Theme.inset
                                border.color: modelData.tone === "accent" ? Theme.accent : Theme.line
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 6; spacing: 0
                                    Text { text: modelData.label; color: Theme.muted; font.family: Theme.family; font.pixelSize: 9; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
                                    Text { text: modelData.value; color: modelData.tone === "accent" ? Theme.accent : Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.bold: true; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight }
                                }
                            }
                        }
                    }

                    Item {
                        visible: viewModel.boardAvailable
                        Layout.fillWidth: true
                        implicitHeight: floatingHandContent.implicitHeight + 14
                        ColumnLayout {
                            id: floatingHandContent
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 7; spacing: 4
                            RowLayout {
                                Layout.fillWidth: true
                                Text { text: "手牌"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.bold: true; Layout.fillWidth: true }
                                Text { text: viewModel.displayHandCards.length + " 张"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 10 }
                            }
                            Flow {
                                objectName: "floatingHandFlow"
                                Layout.fillWidth: true
                                width: parent.width
                                spacing: 3
                                Repeater { model: viewModel.displayHandCards; CardFace { required property var modelData; cardData: modelData; mini: true } }
                            }
                        }
                    }

                    Rectangle {
                        visible: viewModel.boardAvailable
                        Layout.fillWidth: true
                        implicitHeight: 28
                        radius: 8
                        color: Theme.inset
                        border.color: Theme.line
                        Text { anchors.fill: parent; anchors.margins: 7; text: viewModel.multiplierSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 10; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                    }

                    Rectangle {
                        id: floatingAdvicePanel
                        objectName: "floatingAdvicePanel"
                        visible: viewModel.boardAvailable
                        Layout.fillWidth: true
                        implicitHeight: floatingAdviceContent.implicitHeight + 16
                        radius: 6
                        color: viewModel.adviceVisible ? Theme.hero : Theme.inset
                        border.color: viewModel.adviceVisible ? Theme.hero : Theme.line
                        ColumnLayout {
                            id: floatingAdviceContent
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 8; spacing: 4
                            Text { text: viewModel.adviceVisible ? "当前建议" : "当前建议"; color: viewModel.adviceVisible ? "#E6F3EE" : Theme.ink; font.family: Theme.family; font.pixelSize: 11; font.bold: true }
                            Flow {
                                objectName: "floatingRecommendationFlow"
                                Layout.fillWidth: true
                                width: parent.width
                                spacing: 3
                                Repeater { model: viewModel.adviceVisible && viewModel.adviceCandidates.length && !viewModel.adviceCandidates[0].pass ? viewModel.adviceCandidates[0].cards : []
                                    CardFace { required property var modelData; cardData: modelData; mini: true }
                                }
                                Text { visible: viewModel.adviceVisible && viewModel.adviceCandidates.length && viewModel.adviceCandidates[0].pass; text: "过牌"; color: "#F3FFFB"; font.family: Theme.family; font.pixelSize: 16; font.bold: true }
                                Text { visible: !viewModel.adviceVisible; text: viewModel.tableAvailable && !viewModel.seatConfirmed ? "待确认" : viewModel.tableCurrentSeat >= 0 && !viewModel.myTurn ? "等待其他玩家" : "等待建议"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 10 }
                            }
                        }
                    }

                    Item {
                        id: floatingGameBoard
                        objectName: "floatingGameBoard"
                        visible: viewModel.boardAvailable
                        Layout.fillWidth: true
                        implicitHeight: floatingBoardContent.implicitHeight
                        ColumnLayout {
                            id: floatingBoardContent
                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; spacing: 5
                            Text { text: viewModel.seatConfirmed ? viewModel.seatOrderLabel : "绝对座位顺序"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 9; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                            SeatPlayRows { objectName: "floatingRecentPlayGrid"; Layout.fillWidth: true; rows: viewModel.displaySeatRows; compact: true }
                        }
                    }

                }
            }
            CardCounter {
                id: floatingCounter
                visible: viewModel.tableAvailable
                objectName: "floatingCardCounter"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 12
                height: implicitHeight
                entries: viewModel.cardCounter
                unseenTotal: viewModel.counterPublicRemainingTotal
                compact: true
            }
        }
    }
}
