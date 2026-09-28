"""Best-effort system tray / status-bar indicator.

GTK4 dropped Gtk.StatusIcon with no replacement of its own; the de facto
standard on Linux desktops today is the StatusNotifierItem protocol,
exposed to Python via the `AyatanaAppIndicator3` (or older `AppIndicator3`)
GObject-Introspection binding, which most Arch desktop environments ship or
have available (`libayatana-appindicator` package). If neither is present
(e.g. a minimal window manager with no tray host), we simply skip the tray
icon rather than failing -- the daemon keeps tracking regardless, and the
window can always be reopened from the application menu / autostart entry.
"""
from __future__ import annotations

import logging

log = logging.getLogger("screentime.tray")


def try_create_indicator(on_show, on_quit):
    """Returns an indicator object (kept alive by the caller) or None."""
    for ns, ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
        try:
            import gi
            gi.require_version(ns, ver)
            module = __import__("gi.repository", fromlist=[ns])
            AppIndicator3 = getattr(module, ns)
            gi.require_version("Gtk", "4.0")
            break
        except Exception:
            AppIndicator3 = None
            continue
    else:
        log.info("No AppIndicator implementation available; skipping tray icon "
                 "(the window can still be reopened from your application launcher).")
        return None

    # AppIndicator3/Ayatana are built against GTK3 menus even in GTK4 apps;
    # we build a tiny GTK3 menu just for the tray, isolated from the main
    # GTK4 UI. This is the standard, widely used pattern for tray support
    # in GTK4 apps until a GTK4-native tray protocol binding matures.
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk as Gtk3
    except Exception:
        log.info("GTK3 (required for the tray menu) not available; skipping tray icon.")
        return None

    indicator = AppIndicator3.Indicator.new(
        "screentime", "screentime", AppIndicator3.IndicatorCategory.APPLICATION_STATUS
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_icon_full("screentime", "ScreenTime")

    menu = Gtk3.Menu()
    show_item = Gtk3.MenuItem(label="Show ScreenTime")
    show_item.connect("activate", lambda *_: on_show())
    menu.append(show_item)
    quit_item = Gtk3.MenuItem(label="Quit")
    quit_item.connect("activate", lambda *_: on_quit())
    menu.append(quit_item)
    menu.show_all()
    indicator.set_menu(menu)
    return indicator
