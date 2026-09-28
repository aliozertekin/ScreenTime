"""A minimal, dependency-free bar chart widget (GTK4 DrawingArea + Cairo).

Deliberately not using a charting library: the app only ever needs simple
vertical bar charts (daily usage over a range), and Cairo is already a
transitive dependency of GTK, so this keeps the runtime dependency list
short as required by the brief.
"""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk
try:
    # Enables passing a real cairo.Context to Gtk.DrawingArea's draw_func.
    # Ships as part of PyGObject on Arch (paired with the `python-cairo`
    # package); on some minimal / dev-only environments the cairo
    # GObject-Introspection override isn't built, so we degrade to an empty
    # chart rather than crashing the whole application over one widget.
    gi.require_foreign("cairo")
    _CAIRO_OK = True
except (ImportError, ValueError):
    _CAIRO_OK = False


class BarChart(Gtk.DrawingArea):
    def __init__(self, height: int = 160):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self._labels: list[str] = []
        self._values: list[float] = []
        self._value_fmt = str
        self.set_draw_func(self._draw)

    def set_data(self, labels: list[str], values: list[float], value_fmt=str):
        self._labels = labels
        self._values = values
        self._value_fmt = value_fmt
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        if not _CAIRO_OK:
            return
        style = self.get_style_context()
        accent = Gdk.RGBA()
        accent.parse("#3584e4")
        fg = Gdk.RGBA()
        fg.parse("#5e5c64")

        if not self._values:
            return
        max_v = max(self._values) or 1.0
        n = len(self._values)
        margin_bottom = 28
        margin_top = 18
        plot_h = height - margin_bottom - margin_top
        gap = 6
        bar_w = max(4, (width - gap * (n + 1)) / n)

        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.9)
        for i, v in enumerate(self._values):
            bar_h = (v / max_v) * plot_h if max_v else 0
            x = gap + i * (bar_w + gap)
            y = margin_top + (plot_h - bar_h)
            self._rounded_rect(cr, x, y, bar_w, bar_h, 4)
            cr.fill()

        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        cr.set_font_size(11)
        for i, label in enumerate(self._labels):
            x = gap + i * (bar_w + gap)
            extents = cr.text_extents(label)
            cr.move_to(x + bar_w / 2 - extents.width / 2, height - 8)
            cr.show_text(label)

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        if h <= 0:
            return
        r = min(r, w / 2, h / 2) if h > 2 * r else h / 2
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708) if h > r else None
        cr.line_to(x + r, y + h)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.14159) if h > r else None
        cr.arc(x + r, y + r, r, 3.14159, 4.71239)
        cr.close_path()
