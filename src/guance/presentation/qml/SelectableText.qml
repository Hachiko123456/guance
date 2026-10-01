import QtQuick
import QtQuick.Layouts
import QtQuick.Controls.Basic

TextArea {
    id: control

    property int maxVisibleHeight: 132
    property int minVisibleHeight: 22

    readOnly: true
    selectByMouse: true
    selectByKeyboard: true
    persistentSelection: true
    activeFocusOnPress: true
    wrapMode: TextEdit.Wrap
    textFormat: TextEdit.PlainText
    clip: true
    padding: 0
    background: null
    implicitHeight: Math.min(maxVisibleHeight, Math.max(minVisibleHeight, contentHeight))
    Layout.minimumHeight: minVisibleHeight
    Layout.preferredHeight: implicitHeight
    Layout.maximumHeight: maxVisibleHeight

    function selectAllText() {
        control.forceActiveFocus()
        control.selectAll()
    }

    // A passive grab leaves native text dragging and single-click focus intact.
    // Defer selection until the TextArea finishes its own double-click handling,
    // which otherwise can replace our full selection with its word selection.
    TapHandler {
        objectName: "selectableTextDoubleClickHandler"
        acceptedButtons: Qt.LeftButton
        gesturePolicy: TapHandler.DragThreshold
        onDoubleTapped: Qt.callLater(function() {
            control.selectAllText()
        })
    }

    MouseArea {
        objectName: "selectableTextPointerSurface"
        anchors.fill: parent
        acceptedButtons: Qt.RightButton
        onPressed: function(mouse) {
            control.forceActiveFocus()
            contextMenu.popup()
            mouse.accepted = true
        }
    }

    Menu {
        id: contextMenu
        objectName: "selectableTextContextMenu"
        MenuItem {
            objectName: "selectableTextCopyMenuItem"
            text: "复制"
            enabled: control.selectedText.length > 0
            onTriggered: control.copy()
        }
    }
}
