// ScreenTime one-shot focus announcement -- KWin script (Plasma 5/6).
//
// Loaded briefly by the daemon right after it starts (then unloaded). The
// persistent script (main.js) only reports focus *changes*, and at login it
// loads before the daemon exists, so the window that is already focused would
// otherwise go uncounted until the next switch. This reports the current
// window once. It is deliberately a separate script: it can never disturb the
// persistent one, so a failure here cannot break tracking.
(function () {
    var win = workspace.activeWindow || workspace.activeClient;
    if (!win) {
        print("screentime-announce: no active window");
        return;
    }
    try {
        callDBus(
            "org.screentime.KWinFocus",
            "/org/screentime/KWinFocus",
            "org.screentime.KWinFocus",
            "ReportFocus",
            JSON.stringify({
                resourceClass: win.resourceClass || "",
                pid: win.pid || 0,
                caption: win.caption || "",
            })
        );
        print("screentime-announce: reported " + (win.resourceClass || "?"));
    } catch (e) {
        print("screentime-announce: callDBus failed: " + e);
    }
})();
