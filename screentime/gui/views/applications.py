from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GObject

from ... import stats
from ...db import Database
from ..widgets.app_row import AppUsageRow
from ..widgets.bar_chart import BarChart


class ApplicationsView(Gtk.Box):
    def __init__(self, db: Database, navigation_view: Adw.NavigationView):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.db = db
        self.navigation_view = navigation_view
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        title = Gtk.Label(label="Applications", xalign=0)
        title.add_css_class("title-1")
        self.append(title)

        subtitle = Gtk.Label(label="All-time usage. Click an app for full details.", xalign=0)
        subtitle.add_css_class("dim-label")
        self.append(subtitle)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.group = Adw.PreferencesGroup()
        scroller.set_child(self.group)
        self.append(scroller)

        self.refresh()

    def refresh(self):
        for row in getattr(self, "_rows", []):
            self.group.remove(row)
        self._rows = []

        summary = stats.all_time_summary(self.db)
        if not summary.apps:
            placeholder = Adw.ActionRow(title="No applications tracked yet")
            self.group.add(placeholder)
            self._rows.append(placeholder)
            return

        max_seconds = summary.apps[0].seconds
        for app in summary.apps:
            row = Adw.ActionRow(activatable=True)
            usage_row = AppUsageRow(app.display_name, app.icon_name, app.seconds, max_seconds, app.percent)
            row.set_child(usage_row)
            row.connect("activated", lambda r, a=app: self._open_detail(a.app_id))
            self.group.add(row)
            self._rows.append(row)

    def _open_detail(self, app_id: int):
        page = AppDetailPage(self.db, app_id, self.navigation_view)
        self.navigation_view.push(page)


class AppDetailPage(Adw.NavigationPage):
    def __init__(self, db: Database, app_id: int, navigation_view: Adw.NavigationView):
        detail = stats.app_detail(db, app_id)
        super().__init__(title=detail.display_name)
        self.db = db
        self.app_id = app_id

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        for label, value in [
            ("Today", stats.format_duration(detail.today_seconds)),
            ("Last 7 days", stats.format_duration(detail.last7_seconds)),
            ("This month", stats.format_duration(detail.month_seconds)),
            ("All time", stats.format_duration(detail.all_time_seconds)),
        ]:
            stats_row.append(self._stat_card(label, value))
        content.append(stats_row)

        meta_group = Adw.PreferencesGroup(title="Session statistics")
        meta_group.add(Adw.ActionRow(title="Number of sessions", subtitle=str(detail.session_count)))
        meta_group.add(Adw.ActionRow(title="Average session length",
                                      subtitle=stats.format_duration(detail.avg_session_seconds)))
        meta_group.add(Adw.ActionRow(title="Longest session",
                                      subtitle=stats.format_duration(detail.longest_session_seconds)))
        content.append(meta_group)

        chart_group = Adw.PreferencesGroup(title="Daily usage this month")
        chart = BarChart(height=140)
        labels = [d[5:] for d, _ in detail.daily]  # MM-DD
        values = [v for _, v in detail.daily]
        chart.set_data(labels, values)
        chart_row = Adw.PreferencesRow()
        chart_row.set_child(chart)
        chart_group.add(chart_row)
        content.append(chart_group)

        sessions_group = Adw.PreferencesGroup(title="Today's sessions")
        if not detail.sessions_today:
            sessions_group.add(Adw.ActionRow(title="No sessions today"))
        else:
            import datetime
            for start, end, dur in detail.sessions_today:
                t1 = datetime.datetime.fromtimestamp(start).strftime("%H:%M")
                t2 = datetime.datetime.fromtimestamp(end).strftime("%H:%M")
                sessions_group.add(Adw.ActionRow(
                    title=f"{t1} \u2013 {t2}", subtitle=stats.format_duration(dur)
                ))
        content.append(sessions_group)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(content)
        toolbar_view.set_content(scroller)
        self.set_child(toolbar_view)

    def _stat_card(self, title: str, value: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("dim-label")
        v = Gtk.Label(label=value, xalign=0)
        v.add_css_class("title-3")
        inner.append(t)
        inner.append(v)
        box.append(inner)
        return box
