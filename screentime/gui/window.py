from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..db import Database
from ..config import Config
from .views.dashboard import DashboardView
from .views.applications import ApplicationsView
from .views.history import HistoryView
from .views.statistics import StatisticsView
from .views.settings import SettingsView

NAV_ITEMS = [
    ("dashboard", "Dashboard", "go-home-symbolic"),
    ("applications", "Applications", "view-grid-symbolic"),
    ("history", "History", "document-open-recent-symbolic"),
    ("statistics", "Statistics", "org.gnome.Charts-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, db: Database):
        super().__init__(application=app, title="ScreenTime", default_width=980, default_height=680)
        self.db = db
        self.config = Config(db)

        split_view = Adw.NavigationSplitView()
        self.set_content(split_view)

        # ---- Sidebar --------------------------------------------------------
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(show_end_title_buttons=False)
        sidebar_header.set_title_widget(Adw.WindowTitle(title="ScreenTime"))
        sidebar_toolbar.add_top_bar(sidebar_header)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        for key, label, icon in NAV_ITEMS:
            row = Adw.ActionRow(title=label)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            row.set_activatable(True)
            row._nav_key = key
            self.listbox.append(row)
        self.listbox.connect("row-activated", self._on_nav_selected)
        sidebar_toolbar.set_content(self.listbox)

        sidebar_page = Adw.NavigationPage(title="ScreenTime", child=sidebar_toolbar)
        split_view.set_sidebar(sidebar_page)

        # ---- Content: a NavigationView so Applications can push a detail page ----
        self.content_nav = Adw.NavigationView()
        self.main_toolbar = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        self.main_toolbar.add_top_bar(self.content_header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.dashboard_view = DashboardView(db)
        self.applications_view = ApplicationsView(db, self.content_nav)
        self.history_view = HistoryView(db)
        self.statistics_view = StatisticsView(db)
        self.settings_view = SettingsView(db, self.config)

        self.stack.add_named(self.dashboard_view, "dashboard")
        self.stack.add_named(self.applications_view, "applications")
        self.stack.add_named(self.history_view, "history")
        self.stack.add_named(self.statistics_view, "statistics")
        self.stack.add_named(self.settings_view, "settings")

        self.main_toolbar.set_content(self.stack)
        main_page = Adw.NavigationPage(title="ScreenTime", child=self.main_toolbar)
        self.content_nav.add(main_page)
        content_page = Adw.NavigationPage(title="Content", child=self.content_nav)
        split_view.set_content(content_page)

        self.listbox.select_row(self.listbox.get_row_at_index(0))

        # Refresh visible data periodically so the GUI reflects the daemon's
        # writes live, without the GUI itself doing any tracking.
        GLib.timeout_add_seconds(5, self._periodic_refresh)

    def _on_nav_selected(self, listbox, row):
        key = row._nav_key
        self.stack.set_visible_child_name(key)
        self._refresh_current()

    def _refresh_current(self):
        name = self.stack.get_visible_child_name()
        if name == "dashboard":
            self.dashboard_view.refresh()
        elif name == "applications":
            self.applications_view.refresh()
        elif name == "history":
            self.history_view.refresh()
        elif name == "statistics":
            self.statistics_view.refresh()
        elif name == "settings":
            self.settings_view.refresh()

    def _periodic_refresh(self) -> bool:
        self._refresh_current()
        return True
