// ScreenTime Focus Bridge -- GNOME Shell extension (GNOME 45+, ESM style)
//
// Purpose: GNOME Wayland deliberately exposes no client-facing API for
// "what window is currently focused" (this is intentional Wayland
// sandboxing). This tiny extension runs *inside* the shell process, where
// that information already lives (global.display.focus_window), and relays
// it over the session D-Bus so the ScreenTime daemon (an ordinary,
// unprivileged process) can read it -- and nothing else. It performs no
// network access and stores nothing.
//
// Install: copy this directory to
//   ~/.local/share/gnome-shell/extensions/screentime-focus@screentime.local/
// then log out/in (or `busctl --user call org.gnome.Shell ... Eval` on X11
// nested sessions) and enable it with:
//   gnome-extensions enable screentime-focus@screentime.local

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';

const IFACE_XML = `
<node>
  <interface name="org.gnome.Shell.Extensions.ScreenTime">
    <method name="GetFocusedWindow">
      <arg type="s" direction="out" name="json"/>
    </method>
  </interface>
</node>`;

export default class ScreenTimeFocusExtension extends Extension {
    enable() {
        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbusImpl.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/ScreenTime');
    }

    disable() {
        if (this._dbusImpl) {
            this._dbusImpl.unexport();
            this._dbusImpl = null;
        }
    }

    GetFocusedWindow() {
        try {
            const win = global.display.focus_window;
            if (!win) {
                return JSON.stringify({});
            }
            const tracker = Shell.WindowTracker.get_default();
            const app = tracker.get_window_app(win);
            const appId = app ? app.get_id().replace(/\.desktop$/, '') : (win.get_wm_class() || '');
            return JSON.stringify({
                app_id: appId,
                pid: win.get_pid(),
                title: win.get_title() || '',
            });
        } catch (e) {
            return JSON.stringify({});
        }
    }
}
