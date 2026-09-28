from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ... import stats
from ...db import Database
from ..widgets.app_row import AppUsageRow


class StatisticsView(Gtk.Box):
    def __init__(self, db: Database):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.db = db
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        title = Gtk.Label(label="Statistics", xalign=0)
        title.add_css_class("title-1")
        self.append(title)

        cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.today_card = self._card("Today")
        self.week_card = self._card("This week")
        self.month_card = self._card("This month")
        self.alltime_card = self._card("All time")
        for c in (self.today_card, self.week_card, self.month_card, self.alltime_card):
            cards.append(c["box"])
        self.append(cards)

        self.ranking_selector = Gtk.DropDown.new_from_strings(
            ["Today", "This week", "This month", "All time"]
        )
        self.ranking_selector.set_selected(3)
        self.ranking_selector.connect("notify::selected", lambda *_: self.refresh())
        self.append(self.ranking_selector)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.group = Adw.PreferencesGroup(title="Ranking")
        scroller.set_child(self.group)
        self.append(scroller)

        self._rows = []
        self.refresh()

    def _card(self, title: str) -> dict:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(14)
        inner.set_margin_bottom(14)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("dim-label")
        v = Gtk.Label(xalign=0)
        v.add_css_class("title-2")
        inner.append(t)
        inner.append(v)
        box.append(inner)
        return {"box": box, "value": v}

    def refresh(self):
        today = stats.today_summary(self.db)
        week = stats.week_summary(self.db)
        month = stats.month_summary(self.db)
        alltime = stats.all_time_summary(self.db)
        self.today_card["value"].set_label(stats.format_duration(today.total_seconds))
        self.week_card["value"].set_label(stats.format_duration(week.total_seconds))
        self.month_card["value"].set_label(stats.format_duration(month.total_seconds))
        self.alltime_card["value"].set_label(stats.format_duration(alltime.total_seconds))

        idx = self.ranking_selector.get_selected()
        summary = [today, week, month, alltime][idx]

        for row in self._rows:
            self.group.remove(row)
        self._rows = []

        if not summary.apps:
            placeholder = Adw.ActionRow(title="No usage recorded in this range")
            self.group.add(placeholder)
            self._rows.append(placeholder)
            return

        max_seconds = summary.apps[0].seconds
        for rank, app in enumerate(summary.apps, start=1):
            row = Adw.ActionRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rank_lbl = Gtk.Label(label=f"#{rank}")
            rank_lbl.add_css_class("dim-label")
            rank_lbl.set_width_chars(3)
            box.append(rank_lbl)
            usage_row = AppUsageRow(app.display_name, app.icon_name, app.seconds, max_seconds, app.percent)
            usage_row.set_hexpand(True)
            box.append(usage_row)
            row.set_child(box)
            self.group.add(row)
            self._rows.append(row)
