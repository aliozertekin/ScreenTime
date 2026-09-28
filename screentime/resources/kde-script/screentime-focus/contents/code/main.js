// ScreenTime Focus Bridge -- KWin script (Plasma 6 / Wayland)
//
// KWin scripts run inside the compositor, where "what window is focused"
// is naturally known, but they cannot host their own D-Bus service. What
// they CAN do is call out to an existing D-Bus method whenever a window is
// activated. The ScreenTime daemon (an ordinary background process) hosts
// a tiny local service, org.screentime.KWinFocus, specifically to receive
// this push. This script does nothing else: no network access, no storage.
//
// Install (Plasma 6):
//   kpackagetool6 --type KWin/Script -i /path/to/screentime-focus
//   kwriteconfig6 --file kwinrc --group Plugins --key screentime-focusEnabled true
//   qdbus org.kde.KWin /KWin reconfigure
//
// Plasma 5 fallback: the activation signal is `workspace.clientActivated`
// and the object is called a "client" rather than a "window", but the
// properties used below (resourceClass, pid, caption) are the same.
//
// Debugging: this script prints to KWin's own log at every important step
// (load, connect, each activation, each D-Bus call outcome) specifically
// because a silent failure here is otherwise invisible from the ScreenTime
// daemon's side -- it just sees "nothing ever arrives" with no way to tell
// whether the script never loaded, connected to the wrong/no signal, or
// loaded and fired but the D-Bus call itself failed. Read this output with:
//   journalctl -f SYSLOG_IDENTIFIER=kwin_wayland
// (or kwin_x11 on an X11 session) and look for lines starting "screentime-focus:".

print("screentime-focus: script loading");

function reportFocus(win) {
    if (!win) {
        print("screentime-focus: activation fired with no window (focus cleared)");
        return;
    }
    var resourceClass = win.resourceClass || "";
    var pid = win.pid || 0;
    print("screentime-focus: activated resourceClass=" + resourceClass + " pid=" + pid);
    try {
        var payload = JSON.stringify({
            resourceClass: resourceClass,
            pid: pid,
            caption: win.caption || "",
        });
        callDBus(
            "org.screentime.KWinFocus",
            "/org/screentime/KWinFocus",
            "org.screentime.KWinFocus",
            "ReportFocus",
            payload
        );
        print("screentime-focus: callDBus sent ok");
    } catch (e) {
        // Printed rather than swallowed: a daemon that isn't running right
        // now is an expected, harmless case, but this is also the only
        // place a genuine callDBus/signature problem would ever surface.
        print("screentime-focus: callDBus failed: " + e);
    }
}

try {
    if (typeof workspace.windowActivated !== "undefined") {
        // Plasma 6
        print("screentime-focus: connecting to workspace.windowActivated (KWin 6 API)");
        workspace.windowActivated.connect(reportFocus);
        if (workspace.activeWindow) {
            reportFocus(workspace.activeWindow);
        }
    } else if (typeof workspace.clientActivated !== "undefined") {
        // Plasma 5 fallback
        print("screentime-focus: connecting to workspace.clientActivated (KWin 5 API)");
        workspace.clientActivated.connect(reportFocus);
        if (workspace.activeClient) {
            reportFocus(workspace.activeClient);
        }
    } else {
        print("screentime-focus: ERROR -- neither workspace.windowActivated nor "
              + "workspace.clientActivated exist on this KWin version; focus "
              + "tracking cannot work until this script is updated for it");
    }
    print("screentime-focus: script loaded successfully");
} catch (e) {
    print("screentime-focus: ERROR during setup: " + e);
}
