"""
Automatically installs and enables the Wayland focus-detection companion
piece appropriate for the current desktop (the GNOME Shell extension or the
KWin script -- see window_detector.py's module docstring for why these
exist at all: GNOME and KDE expose no client-facing API for "what window is
focused" on Wayland, by design).

The source files are bundled *inside* the Python package
(screentime/resources/) specifically so this works no matter how screentime
was installed -- `pip install`, the Arch package, or a manual venv all put
these files somewhere importlib.resources can find, without needing a
separate /usr/share copy step to stay in sync.

This is invoked automatically at daemon/GUI startup whenever the current
session is on a Wayland desktop that needs one. Rather than a simple
"attempted once, never again" flag, it's gated by a content hash of the
bundled resource files, stored per-desktop in the settings table: if the
bundled content (i.e. the installed screentime *package* version) differs
from whatever hash was recorded after the last successful/attempted
install, it reinstalls automatically. This matters because a plain
one-shot flag caused a real, confusing bug in practice: an early, broken
copy got installed once, the flag got set, and every later screentime
*package* upgrade that fixed the companion piece's content silently had no
effect on the already-broken on-disk copy -- nothing ever told the
installer the bundled content had changed, so it just kept skipping,
looking successful in every check except the one that actually mattered
(kpackagetool6 loading it). Hashing the bundled content instead means a
package upgrade with different companion-piece files self-heals on the very
next daemon/GUI start, with no manual "Reinstall" click required.

It's also exposed as a manual "Install / Reinstall" action in Settings ->
Diagnostics, primarily for retrying after an environmental failure (e.g.
kpackagetool6 wasn't on PATH yet) rather than a content change, since that
case doesn't change the hash and so isn't automatically retried on every
startup (to avoid retry-spamming a persistent, unrelated failure).
"""
from __future__ import annotations

import hashlib
import importlib.resources as resources
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("screentime.wayland_setup")


def _hash_resource_dir(resource_dir) -> str:
    """Stable hash of a bundled resource directory's file contents, used to
    detect when the *bundled* companion piece has changed (e.g. after a
    screentime package upgrade) so it can be refreshed automatically rather
    than relying on the user noticing and clicking "Reinstall"."""
    h = hashlib.sha256()
    with resources.as_file(resource_dir) as path:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(path)).encode())
                h.update(p.read_bytes())
    return h.hexdigest()


def detect_wayland_desktop() -> Optional[str]:
    """Returns 'kde', 'gnome', or None (not applicable -- X11, sway,
    Hyprland, or an unrecognized desktop, none of which need a companion
    piece: sway/Hyprland are handled natively, X11 is handled natively)."""
    if os.environ.get("XDG_SESSION_TYPE") != "wayland":
        return None
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    if "kde" in desktop or "plasma" in desktop:
        return "kde"
    if "gnome" in desktop:
        return "gnome"
    return None


# ------------------------------------------------------------------- KDE
def _kde_script_resource_dir():
    return resources.files("screentime") / "resources" / "kde-script" / "screentime-focus"


def _kde_script_dest() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(xdg_data) / "kwin/scripts/screentime-focus"


def kde_script_is_installed() -> bool:
    # Checked directly on disk rather than by parsing `kpackagetool6 --list`
    # output: that command's success/format-recognition can depend on the
    # exact metadata.json schema kpackagetool6 expects (which has bitten us
    # before), and it prints some diagnostics to stderr rather than stdout,
    # so a substring check on stdout alone can under-report. The directory
    # existing with its metadata.json is the actual ground truth here.
    return (_kde_script_dest() / "metadata.json").exists()


def kde_script_metadata_valid(dest: Optional[Path] = None) -> bool:
    """A packaged KWin 6 script is only auto-loaded at session start if its
    metadata.json declares "KPackageStructure": "KWin/Script". Without it the
    package installs fine and even runs when pushed live via Scripting.loadScript
    -- but KWin never discovers it at login, so tracking silently dies at every
    reboot (kpackagetool6 --list shows 'does not match requested format').
    Being *present on disk* is therefore not proof it will load."""
    import json
    try:
        meta = json.loads(((dest or _kde_script_dest()) / "metadata.json").read_text())
    except Exception:
        return False
    return meta.get("KPackageStructure") == "KWin/Script"


def kde_script_is_up_to_date() -> bool:
    """True only if something is installed AND its content matches exactly
    what's currently bundled in this screentime package. False both when
    nothing is installed and when a stale/different copy is (e.g. from
    before a bug fix shipped in a later screentime version) -- both cases
    mean "an install is needed", which is exactly what auto_install_if_needed
    uses this for."""
    dest = _kde_script_dest()
    if not (dest / "metadata.json").exists():
        return False
    if not kde_script_metadata_valid(dest):
        return False
    try:
        return _hash_resource_dir(dest) == _hash_resource_dir(_kde_script_resource_dir())
    except Exception:
        return False


def _qdbus() -> Optional[str]:
    return shutil.which("qdbus6") or shutil.which("qdbus")


def _kwin_scripting(qdbus: str, member: str, *args: str, path: str = "/Scripting",
                    iface: str = "org.kde.kwin.Scripting"):
    return subprocess.run([qdbus, "org.kde.KWin", path, f"{iface}.{member}", *args],
                          capture_output=True, text=True, timeout=5)


def kwin_load_and_run(qdbus: str, script_path: Path, plugin_name: str) -> bool:
    """Load a script into the running KWin AND start it.

    `Scripting.loadScript` only registers the script and returns its id; it is
    `Script<id>.run` that executes it (this is how kdotool drives KWin too).
    Loading without running leaves a script that looks loaded but never fires.
    `run` on an already-running script is a no-op, so calling it is safe either
    way. Returns True only if both steps succeeded."""
    r = _kwin_scripting(qdbus, "loadScript", str(script_path), plugin_name)
    if r.returncode != 0:
        return False
    try:
        script_id = int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    if script_id < 0:                 # KWin already has a script by that name
        return False
    ran = _kwin_scripting(qdbus, "run", path=f"/Scripting/Script{script_id}",
                          iface="org.kde.kwin.Script")
    return ran.returncode == 0


ANNOUNCE_PLUGIN_NAME = "screentime-announce"


def announce_focus_once(linger_seconds: float = 1.0) -> bool:
    """Have KWin report the currently focused window to the daemon, once.

    Runs a tiny separate script (announce.js) and unloads it again. It never
    touches the persistent `screentime-focus` script, so even if KWin's
    scripting API behaves unexpectedly this cannot break ongoing tracking (an
    earlier version unloaded/reloaded the persistent script and could leave it
    loaded-but-not-running). Local session-bus calls only."""
    qdbus = _qdbus()
    announce = _kde_script_dest() / "contents" / "code" / "announce.js"
    if not qdbus or not announce.exists():
        return False
    try:
        _kwin_scripting(qdbus, "unloadScript", ANNOUNCE_PLUGIN_NAME)   # stale copy from a crash
        ok = kwin_load_and_run(qdbus, announce, ANNOUNCE_PLUGIN_NAME)
        time.sleep(linger_seconds)          # let the D-Bus call go out before unloading
        _kwin_scripting(qdbus, "unloadScript", ANNOUNCE_PLUGIN_NAME)
        return ok
    except Exception as e:
        log.debug("KWin focus announce failed: %s", e)
        return False


def install_kde_script() -> tuple[bool, str]:
    tool = shutil.which("kpackagetool6") or shutil.which("kpackagetool5")
    if not tool:
        return False, "kpackagetool6/kpackagetool5 not found -- is plasma-workspace installed?"

    # Always do a clean reinstall rather than trying to decide between
    # --install and --upgrade: kpackagetool6 can refuse --install if it
    # already knows about the id, and refuse --upgrade if the on-disk
    # metadata doesn't validate as the type it expects -- both real,
    # observed failure modes. Since we fully control this content, removing
    # any existing copy first and always installing fresh sidesteps that
    # entirely and is idempotent.
    dest = _kde_script_dest()
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except Exception as e:
            return False, f"Could not remove existing install at {dest}: {e}"

    try:
        with resources.as_file(_kde_script_resource_dir()) as src_path:
            r = subprocess.run([tool, "--type", "KWin/Script", "--install", str(src_path)],
                                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return False, f"kpackagetool failed: {r.stderr.strip() or r.stdout.strip()}"
    except Exception as e:
        return False, f"Could not install KWin script: {e}"

    kwriteconfig = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if kwriteconfig:
        try:
            subprocess.run([kwriteconfig, "--file", "kwinrc", "--group", "Plugins",
                             "--key", "screentime-focusEnabled", "true"],
                            capture_output=True, text=True, timeout=5)
        except Exception as e:
            log.debug("kwriteconfig failed: %s", e)

    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    reloaded = False
    if qdbus:
        try:
            r = subprocess.run([qdbus, "org.kde.KWin", "/KWin", "reconfigure"],
                                capture_output=True, text=True, timeout=5)
            reloaded = r.returncode == 0
        except Exception as e:
            log.debug("qdbus reconfigure failed: %s", e)

        # `reconfigure` reloads kwinrc's config values, but whether it also
        # forces KWin to re-read an already-loaded script's *code* from disk
        # is unclear from KDE's own docs. The explicitly documented,
        # guaranteed-live mechanism is `Scripting.loadScript`, so use that
        # too as the stronger guarantee -- it loads and starts the fresh
        # content immediately, this session, regardless of whether
        # `reconfigure` alone would have been sufficient. Persistence across
        # future KWin restarts still comes from the kpackagetool6 install +
        # the kwinrc enabled flag set above, not from this call.
        main_script = dest / "contents" / "code" / "main.js"
        if main_script.exists():
            try:
                # Replace any copy KWin already has with the fresh content,
                # then load *and run* it (loadScript alone doesn't run it).
                _kwin_scripting(qdbus, "unloadScript", "screentime-focus")
                if kwin_load_and_run(qdbus, main_script, "screentime-focus"):
                    reloaded = True
            except Exception as e:
                log.debug("qdbus loadScript/run failed: %s", e)

    if not kde_script_is_installed():
        return False, ("kpackagetool reported success but the script isn't on disk where "
                       f"expected ({dest}) -- something is still off. Check "
                       "`kpackagetool6 --type KWin/Script --list` output directly.")

    if reloaded:
        return True, ("Installed and loaded live -- focus tracking should work immediately. "
                      "Check `journalctl -f SYSLOG_IDENTIFIER=kwin_wayland` for lines starting "
                      "\"screentime-focus:\" to confirm it's actually running.")
    return True, ("Installed and enabled, but couldn't confirm KWin reloaded it live. "
                  "Logging out and back in will pick it up -- then check "
                  "`journalctl -f SYSLOG_IDENTIFIER=kwin_wayland` for lines starting "
                  "\"screentime-focus:\" to confirm.")


# ----------------------------------------------------------------- GNOME
def _gnome_extension_resource_dir():
    return resources.files("screentime") / "resources" / "gnome-extension" / "screentime-focus@screentime.local"


def _gnome_extension_dest() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(xdg_data) / "gnome-shell/extensions/screentime-focus@screentime.local"


def gnome_extension_is_installed() -> bool:
    return (_gnome_extension_dest() / "metadata.json").exists()


def gnome_extension_is_up_to_date() -> bool:
    dest = _gnome_extension_dest()
    if not (dest / "metadata.json").exists():
        return False
    try:
        return _hash_resource_dir(dest) == _hash_resource_dir(_gnome_extension_resource_dir())
    except Exception:
        return False


def gnome_extension_is_enabled() -> bool:
    tool = shutil.which("gnome-extensions")
    if not tool:
        return False
    try:
        out = subprocess.run([tool, "list", "--enabled"], capture_output=True, text=True, timeout=5)
        return "screentime-focus@screentime.local" in out.stdout
    except Exception:
        return False


def install_gnome_extension() -> tuple[bool, str]:
    dest = _gnome_extension_dest()
    try:
        with resources.as_file(_gnome_extension_resource_dir()) as src_path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src_path, dest)
    except Exception as e:
        return False, f"Could not copy extension files: {e}"

    tool = shutil.which("gnome-extensions")
    if not tool:
        return True, ("Installed, but the 'gnome-extensions' command isn't available to enable it "
                      "automatically. Enable it from GNOME Extensions app, or run: "
                      "gnome-extensions enable screentime-focus@screentime.local")
    try:
        r = subprocess.run([tool, "enable", "screentime-focus@screentime.local"],
                            capture_output=True, text=True, timeout=5)
    except Exception as e:
        return True, f"Installed, but enabling failed: {e}"

    if r.returncode == 0:
        # On Wayland, GNOME Shell can only load a *new* extension's code
        # after the shell process restarts (Alt+F2 r on X11 doesn't apply
        # to Wayland sessions) -- logging out and back in is the reliable way.
        return True, ("Installed and enabled. GNOME Shell on Wayland only picks up a brand-new "
                      "extension's code after logging out and back in -- please do that once, "
                      "then focus tracking will work.")
    return True, (f"Installed, but couldn't auto-enable ({r.stderr.strip()}). Enable it with: "
                  "gnome-extensions enable screentime-focus@screentime.local")


# --------------------------------------------------------------- dispatch
def install_for_current_desktop() -> tuple[bool, str]:
    desktop = detect_wayland_desktop()
    if desktop == "kde":
        return install_kde_script()
    if desktop == "gnome":
        return install_gnome_extension()
    return False, "No companion piece is needed (or applicable) for this session."


def status_for_current_desktop() -> Optional[dict]:
    """Returns diagnostic info for the Settings page, or None if not applicable."""
    desktop = detect_wayland_desktop()
    if desktop == "kde":
        return {
            "desktop": "kde",
            "installed": kde_script_is_installed(),
            "up_to_date": kde_script_is_up_to_date(),
        }
    if desktop == "gnome":
        return {
            "desktop": "gnome",
            "installed": gnome_extension_is_installed(),
            "up_to_date": gnome_extension_is_up_to_date(),
            "enabled": gnome_extension_is_enabled(),
        }
    return None


def auto_install_if_needed(db) -> Optional[tuple[bool, str]]:
    """Called at daemon/GUI startup. Installs+enables the companion piece
    for the current desktop whenever what's on disk doesn't already match
    what's bundled in this screentime package -- covering both "never
    installed" and "an older/stale copy is installed from before a fix
    shipped" without needing a manual "Reinstall" click for the latter.
    A failed attempt is remembered per exact bundled-content-version (not
    forever) so a persistent environmental problem (e.g. kpackagetool6
    missing) doesn't retry -- and potentially spam logs/D-Bus calls -- on
    every single startup; it tries again automatically the next time the
    bundled content actually changes (i.e. the next screentime upgrade)."""
    desktop = detect_wayland_desktop()
    if desktop is None:
        return None

    if desktop == "kde":
        up_to_date = kde_script_is_up_to_date()
        bundled_hash = _hash_resource_dir(_kde_script_resource_dir())
    else:
        up_to_date = gnome_extension_is_up_to_date()
        bundled_hash = _hash_resource_dir(_gnome_extension_resource_dir())

    if up_to_date:
        return None

    failed_hash_key = f"wayland_helper_last_failed_hash_{desktop}"
    if db.get_setting(failed_hash_key) == bundled_hash:
        return None  # already tried this exact bundled version and it failed; don't spam-retry

    ok, msg = install_for_current_desktop()
    db.set_setting(failed_hash_key, "" if ok else bundled_hash)
    log.info("Wayland focus helper auto-install (%s): %s -- %s", desktop, "ok" if ok else "failed", msg)
    return ok, msg
