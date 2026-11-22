import QtQuick

Rectangle {
    id: card
    property var cardData: ({rank: "A", suit: "♠", red: false, wild: false, joker: false, label: "♠A"})
    property bool compact: false
    property bool mini: false
    property bool publicPlay: false
    property int displayWidth: 0
    property int displayHeight: 0
    readonly property bool dense: displayWidth > 0
    implicitWidth: dense ? displayWidth : publicPlay ? 28 : mini ? 38 : compact ? 47 : 86
    implicitHeight: dense ? displayHeight : publicPlay ? 42 : mini ? 52 : compact ? 65 : 122
    radius: dense ? 3 : compact || mini ? 5 : 9
    color: "#FFFEFC"
    border.color: cardData.wild ? "#53AB88" : "#DFE5E5"
    border.width: cardData.wild ? 2 : 1
    Accessible.role: Accessible.StaticText
    Accessible.name: cardData.label || "牌"
    readonly property color cardInk: cardData.red ? "#C34A43" : "#22363C"
    Text {
        objectName: "cardRank"
        anchors.left: parent.left; anchors.top: parent.top
        anchors.leftMargin: card.dense ? 1 : card.publicPlay ? 3 : card.mini ? 5 : card.compact ? 6 : 10
        anchors.topMargin: card.dense ? 0 : card.publicPlay ? 2 : card.mini ? 3 : card.compact ? 4 : 7
        text: card.cardData.rank
        font.family: card.cardData.joker ? Theme.family : "Georgia"
        font.pixelSize: card.dense ? Math.max(8, Math.min(16, Math.floor(card.width * (card.cardData.joker ? 0.44 : 0.57)))) : card.cardData.joker ? (card.mini ? 10 : card.compact ? 11 : 18) : card.publicPlay ? 14 : card.mini ? 16 : card.compact ? 19 : 30
        font.weight: Font.DemiBold
        color: card.cardInk
    }
    Text {
        objectName: "cardSuit"
        anchors.right: card.publicPlay ? undefined : parent.right
        anchors.left: card.publicPlay ? parent.left : undefined
        anchors.leftMargin: 3
        anchors.bottom: parent.bottom
        anchors.rightMargin: card.dense ? 1 : card.mini ? 5 : 9
        anchors.bottomMargin: card.dense ? 0 : card.mini ? 2 : 6
        text: card.cardData.joker ? "✦" : card.cardData.suit
        font.family: "Segoe UI Symbol"
        font.pixelSize: card.dense ? Math.max(8, Math.min(15, Math.floor(card.height * 0.51))) : card.publicPlay ? 17 : card.mini ? 19 : card.compact ? 27 : 49
        color: card.cardInk
    }
    Rectangle {
        visible: card.cardData.wild && !card.mini && !card.dense
        anchors.right: parent.right; anchors.top: parent.top
        anchors.margins: card.compact ? 3 : 6
        width: 16; height: 16; radius: 4
        color: "#DDEFE4"
        Text { anchors.centerIn: parent; text: "配"; font.family: Theme.family; font.pixelSize: 10; color: "#276D52" }
    }
}
