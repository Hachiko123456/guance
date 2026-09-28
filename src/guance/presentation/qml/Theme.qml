pragma Singleton
import QtQuick

QtObject {
    property bool dark: false
    readonly property string family: "Microsoft YaHei UI"
    readonly property color canvas: dark ? "#141B20" : "#F5F7F8"
    readonly property color panel: dark ? "#1C262C" : "#FFFFFF"
    readonly property color sidebar: dark ? "#182126" : "#FAFBFC"
    readonly property color inset: dark ? "#232F36" : "#F2F5F6"
    readonly property color line: dark ? "#33424B" : "#E5EAED"
    readonly property color ink: dark ? "#EDF4F4" : "#172D35"
    readonly property color muted: dark ? "#A7BAC3" : "#647983"
    readonly property color faint: dark ? "#8298A3" : "#7D919B"
    readonly property color accent: dark ? "#62DCC2" : "#087F70"
    readonly property color accentSoft: dark ? "#213D3A" : "#E5F4EF"
    readonly property color accentText: dark ? "#072E29" : "#FFFFFF"
    readonly property color danger: dark ? "#FFA69D" : "#B74439"
    readonly property color dangerSoft: dark ? "#422D2E" : "#FBEFEC"
    readonly property color amber: dark ? "#ECC183" : "#987134"
    readonly property color amberSoft: dark ? "#3A3428" : "#FBF4E7"
    readonly property color hero: dark ? "#183F3B" : "#173F3B"
    function toneColor(tone) {
        if (tone === "danger") return danger
        if (tone === "success") return accent
        if (tone === "warning") return amber
        return muted
    }
}
