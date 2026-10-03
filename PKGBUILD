# Maintainer: You <you@example.com>
pkgname=screentime
pkgver=1.3.0
pkgrel=1
pkgdesc="A native, fully local application usage tracker (Screen Time for Linux)"
arch=('any')
url="https://github.com/screentime/screentime"
license=('MIT')
install=screentime.install
depends=(
    'python'
    'python-psutil'
    'python-gobject'
    'python-cairo'
    'python-cryptography'   # AES-256-GCM for the protected database (official repo; no AUR needed)
    'gtk4'
    'libadwaita'
    'python-xlib'          # X11 active-window/idle detection
    'xorg-xprop'            # X11 fallback focus lookup when python-xlib unavailable
    'xprintidle'            # X11 idle detection
    'systemd'                # logind D-Bus signals + systemd --user service
)
optdepends=(
    'libayatana-appindicator: system tray icon'
    'sway: native Wayland focus detection under sway'
    'hyprland: native Wayland focus detection under Hyprland'
    'gnome-shell: install the bundled GNOME Shell extension for Wayland focus detection'
    'plasma-workspace: install the bundled KWin script for Wayland focus detection'
    'kwallet: keep the database key in KWallet (enable its Secret Service interface); any Secret Service provider works'
    'gnome-keyring: keep the database key in GNOME Keyring'
)
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
# This PKGBUILD builds straight from the source tree it ships alongside --
# there is no separate tarball to fetch. Run `makepkg -si` from inside the
# extracted project root, in the same directory as this file.
source=()
sha256sums=()

build() {
    cd "$startdir"
    mkdir -p "$srcdir"
    python -m build --wheel --no-isolation --outdir "$srcdir"
}

package() {
    cd "$startdir"
    python -m installer --no-compile --destdir="$pkgdir" "$srcdir"/*.whl

    # systemd --user service
    install -Dm644 data/screentime-daemon.service \
        "$pkgdir/usr/lib/systemd/user/screentime-daemon.service"

    # Desktop entry + icon
    install -Dm644 data/org.screentime.App.desktop \
        "$pkgdir/usr/share/applications/org.screentime.App.desktop"
    install -Dm644 data/icons/screentime.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/screentime.svg"

    # Reference (disabled-by-default) XDG autostart entry -- Settings > "Start
    # at login" is the primary way users enable this; this file is provided
    # for manual/scripted setups and non-systemd sessions.
    install -Dm644 data/screentime-daemon.desktop \
        "$pkgdir/etc/xdg/autostart/screentime-daemon.desktop"

    # The GNOME extension and KWin script are now bundled inside the Python
    # package itself (screentime/resources/) and installed/enabled
    # automatically on first run (see README "Wayland compatibility") --
    # they no longer need a separate /usr/share copy step here.

    install -Dm644 README.md "$pkgdir/usr/share/doc/screentime/README.md"
}
