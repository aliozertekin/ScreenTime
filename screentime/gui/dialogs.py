"""Small shared helpers for dialogs and file pickers (libadwaita 1.2+ / GTK 4.x)."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk


def confirm(parent, heading: str, body: str, ok_label: str, on_ok, extra=None, destructive: bool = False,
            cancel_label: str = "Cancel"):
    """Two-button dialog. `on_ok()` runs only if the user accepts."""
    def on_response(_d, response):
        if response == "ok":
            on_ok()
    if hasattr(Adw, "AlertDialog"):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", cancel_label)
        d.add_response("ok", ok_label)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        if extra is not None:
            d.set_extra_child(extra)
        if destructive:
            d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", on_response)
        d.present(parent)
    else:                                                   # libadwaita < 1.5
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        d.add_response("cancel", cancel_label)
        d.add_response("ok", ok_label)
        d.set_default_response("ok")
        d.set_close_response("cancel")
        if extra is not None:
            d.set_extra_child(extra)
        d.connect("response", on_response)
        d.present()
    return d


def notice(parent, heading: str, body: str, extra=None):
    if hasattr(Adw, "AlertDialog"):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("close", "Close")
        d.set_default_response("close")
        if extra is not None:
            d.set_extra_child(extra)
        d.present(parent)
    else:
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        d.add_response("close", "Close")
        if extra is not None:
            d.set_extra_child(extra)
        d.present()
    return d


def pick_file(parent, title: str, on_path, save: bool = False, initial_name: str = "", filters=()):
    """Ask for a path. `on_path(str)` is called only when the user picks one. `filters`: [(label, [glob...])]."""
    if hasattr(Gtk, "FileDialog"):                          # GTK 4.10+
        dlg = Gtk.FileDialog(title=title)
        if initial_name:
            dlg.set_initial_name(initial_name)
        if filters:
            store = Gio.ListStore.new(Gtk.FileFilter)
            for label, patterns in filters:
                f = Gtk.FileFilter()
                f.set_name(label)
                for pat in patterns:
                    f.add_pattern(pat)
                store.append(f)
            dlg.set_filters(store)

        def done(d, result):
            try:
                gfile = d.save_finish(result) if save else d.open_finish(result)
            except Exception:                               # dismissed
                return
            if gfile is not None and gfile.get_path():
                on_path(gfile.get_path())
        (dlg.save if save else dlg.open)(parent, None, done)
        return dlg
    action = Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN
    native = Gtk.FileChooserNative.new(title, parent, action, None, None)
    if save:
        native.set_current_name(initial_name)

    def resp(n, r):
        if r == Gtk.ResponseType.ACCEPT and n.get_file() is not None:
            on_path(n.get_file().get_path())
    native.connect("response", resp)
    native.show()
    parent._st_native_dialog = native                       # keep a reference alive
    return native
