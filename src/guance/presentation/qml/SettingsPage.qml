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
        title: "选择模型包或 legacy 模型"
        nameFilters: ["模型清单 (manifest.json)", "legacy NumPy 模型 (*.npz)", "模型文件 (*)"]
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

    function modelMetadataValue(key, fallback) {
        var metadata = viewModel.modelMetadata
        if (!metadata || metadata[key] === undefined || metadata[key] === "")
            return fallback
        return String(metadata[key])
    }

    function modelBooleanLabel(key, trueLabel, falseLabel) {
        var metadata = viewModel.modelMetadata || {}
        if (metadata[key] === true) return trueLabel
        if (metadata[key] === false) return falseLabel
        return "未声明"
    }

    function modelCapabilitiesText() {
        var metadata = viewModel.modelMetadata || {}
        var capabilities = metadata.capabilities || {}
        var enabled = []
        for (var key in capabilities) {
            if (capabilities[key]) enabled.push(key)
        }
        return enabled.length ? enabled.join("、") : "未声明"
    }

    function catalogProblemsText() {
        var problems = viewModel.catalogProblems || []
        var parts = []
        for (var index = 0; index < problems.length && index < 3; index++) {
            var problem = problems[index]
            parts.push((problem.directory !== "" ? problem.directory : "目录") + "：" + problem.code)
        }
        return parts.join("；")
    }

    ColumnLayout {
        width: page.availableWidth
        spacing: 18
        Text { text: "设置"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 26; font.weight: Font.DemiBold }
        Text { text: "把复杂设置留在这里，助手页只保留对局需要的信息。"; color: Theme.muted; font.family: Theme.family; font.pixelSize: 13 }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 326; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 7
                Text { text: "模型"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.modelSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelStatusField"; text: "状态：" + modelMetadataValue("status", viewModel.modelState); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelProviderField"; text: "提供方：" + modelMetadataValue("provider", "未声明") + "    后端：" + modelMetadataValue("backend", "未声明"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelRuntimeField"; text: "运行时：" + modelMetadataValue("runtime", "未声明") + " " + modelMetadataValue("runtime_version", "") + "    契约：" + modelMetadataValue("runtime_contract", "未声明") + "    版本：" + modelMetadataValue("model_version", "未声明") + "    迭代：" + modelMetadataValue("iteration", "未声明"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelDeviceField"; visible: text.length > 0; text: modelMetadataValue("device_summary", ""); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelCapabilitiesField"; text: "能力：" + modelCapabilitiesText(); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelExperimentalField"; text: "类型：" + modelBooleanLabel("experimental", "实验性", "常规") + " / " + modelMetadataValue("classification", "未声明") + "    生产就绪：" + modelBooleanLabel("production_ready", "是", "否") + "    强度声明：" + modelBooleanLabel("strength_claim", "是", "否"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelEvaluationField"; text: "正式评估：" + modelMetadataValue("formal_evaluation_status", "未声明") + "    Champion：" + modelMetadataValue("champion_status", "未声明"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelScoreField"; text: "分数：" + (modelMetadataValue("score_kind", "未声明").indexOf("policy_path") >= 0 ? "策略/路径分数（非胜率）" : modelMetadataValue("score_kind", "未声明")); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelFingerprintField"; text: "模型指纹：" + modelMetadataValue("fingerprint", "未声明") + "    制品/包指纹：" + modelMetadataValue("bundle_fingerprint", "未声明"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Text { objectName: "modelDeploymentFingerprintField"; text: "部署指纹：" + modelMetadataValue("deployment_fingerprint", "未声明"); color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                ActionButton { objectName: "importModelButton"; text: "导入模型"; subtle: true; onClicked: modelDialog.open() }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 232; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "模型目录"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true; Layout.fillWidth: true }
                    ActionButton { objectName: "catalogRefreshButton"; text: "刷新"; subtle: true; onClicked: viewModel.request_catalog_refresh() }
                }
                Text {
                    objectName: "activeModelField"
                    text: "当前选择：" + (viewModel.activeModelSummary !== "" ? viewModel.activeModelSummary : "未选择（推荐保持不可用）")
                    color: Theme.muted; font.family: Theme.family; font.pixelSize: 12
                    Layout.fillWidth: true; elide: Text.ElideRight
                }
                Text {
                    objectName: "catalogEmptyField"
                    visible: viewModel.catalogEntries.length === 0
                    text: "未发现模型包。把完整模型包（manifest + artifact + SHA256SUMS）复制到本机模型目录后刷新。"
                    color: Theme.faint; font.family: Theme.family; font.pixelSize: 11
                    Layout.fillWidth: true; wrapMode: Text.WordWrap
                }
                ListView {
                    objectName: "catalogList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    visible: viewModel.catalogEntries.length > 0
                    model: viewModel.catalogEntries
                    delegate: RowLayout {
                        width: ListView.view ? ListView.view.width : 0
                        spacing: 8
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                text: modelData.model_id + "  ·  " + modelData.provider + " / " + modelData.classification
                                color: Theme.ink; font.family: Theme.family; font.pixelSize: 12
                                elide: Text.ElideRight; Layout.fillWidth: true
                            }
                            Text {
                                text: (modelData.experimental ? "实验性" : "常规")
                                      + " · 生产就绪：" + (modelData.production_ready ? "是" : "否")
                                      + (modelData.strength_claim ? " · 强度声明：是" : "")
                                      + (modelData.fingerprint !== "" ? " · " + modelData.fingerprint : "")
                                color: Theme.faint; font.family: Theme.family; font.pixelSize: 10
                                elide: Text.ElideRight; Layout.fillWidth: true
                            }
                        }
                        ActionButton {
                            objectName: "catalogSelectButton-" + modelData.model_id
                            text: viewModel.activeModelId === modelData.model_id ? "已选择" : "选择"
                            subtle: true
                            onClicked: viewModel.request_catalog_model(modelData.model_id)
                        }
                    }
                }
                Text {
                    objectName: "catalogProblemsField"
                    visible: viewModel.catalogProblems.length > 0
                    text: "发现 " + viewModel.catalogProblems.length + " 个不可用条目：" + catalogProblemsText()
                    color: Theme.amber; font.family: Theme.family; font.pixelSize: 10
                    Layout.fillWidth: true; elide: Text.ElideRight
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 142; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "调试报文保存"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true; Layout.fillWidth: true }
                    Switch {
                        objectName: "debugCaptureSwitch"
                        checked: viewModel.debugCaptureEnabled
                        enabled: !viewModel.running && !viewModel.busy
                        onToggled: viewModel.set_debug_capture_enabled(checked)
                    }
                }
                Text {
                    text: "保存白名单 WebSocket 报文到项目目录 debug/captures；下次开始使用时生效。"
                    color: Theme.muted
                    font.family: Theme.family
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Text {
                    text: "仅用于本地调试，单次会话最多 256 MB，不会加入普通诊断包。"
                    color: Theme.faint
                    font.family: Theme.family
                    font.pixelSize: 11
                    Layout.fillWidth: true
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: 166; radius: 14; color: Theme.panel; border.color: Theme.line
            ColumnLayout { anchors.fill: parent; anchors.margins: 20; spacing: 8
                Text { text: "网络组件"; color: Theme.ink; font.family: Theme.family; font.pixelSize: 15; font.bold: true }
                Text { text: viewModel.certificateSummary; color: Theme.muted; font.family: Theme.family; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                Text { visible: viewModel.certificateFingerprint !== ""; text: "指纹：" + viewModel.certificateFingerprint; color: Theme.faint; font.family: Theme.family; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                RowLayout {
                    ActionButton { objectName: "removeCertificateButton"; text: "移除本机授权"; subtle: true; enabled: !viewModel.running && !viewModel.busy; onClicked: removeDialog.open() }
                    ActionButton { objectName: "repairNetworkButton"; text: "修复网络"; subtle: true; onClicked: viewModel.request_repair() }
                }
            }
        }
        Item { Layout.fillHeight: true }
    }
}
