from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ...stats import format_duration


class AppUsageRow(Gtk.Box):
    """One row in a usage list: icon | name | proportional bar | time | %."""

    def __init__(self, display_name: str, icon_name: str | None, seconds: int,
                 max_seconds: int, percent: float, show_percent: bool = True):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(12)
        self.set_margin_end(12)

        image = Gtk.Image()
        image.set_pixel_size(28)
        try:
            if icon_name:
                image.set_from_icon_name(icon_name)
            else:
                image.set_from_icon_name("application-x-executable")
        except Exception:
            image.set_from_icon_name("application-x-executable")
        self.append(image)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        name_lbl = Gtk.Label(label=display_name, xalign=0)
        name_lbl.add_css_class("heading")
        col.append(name_lbl)

        bar = Gtk.ProgressBar()
        frac = (seconds / max_seconds) if max_seconds else 0.0
        bar.set_fraction(min(1.0, frac))
        bar.set_valign(Gtk.Align.CENTER)
        col.append(bar)
        self.append(col)

        time_lbl = Gtk.Label(label=format_duration(seconds), xalign=1)
        time_lbl.add_css_class("numeric")
        time_lbl.set_width_chars(9)
        self.append(time_lbl)

        if show_percent:
            pct_lbl = Gtk.Label(label=f"{percent:.0f}%", xalign=1)
            pct_lbl.add_css_class("dim-label")
            pct_lbl.set_width_chars(5)
            self.append(pct_lbl)
