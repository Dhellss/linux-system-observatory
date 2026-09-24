"""Settings page.

Every control writes through :class:`~app.core.config.ConfigService` immediately,
and the services that care subscribe to change notifications -- so adjusting a
sampling interval or the theme takes effect at once, with no Apply button and no
restart.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from app.core.paths import config_dir, data_dir, export_dir, log_file, state_dir
from app.core.units import bytes_
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import Badge, Divider, KeyValueTable


class SettingsPage(Page):
    """Application preferences."""

    domain = ""
    title = "Settings"
    icon = "⚙"
    section = "Application"
    description = "Appearance, sampling, history, privacy and notifications"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("settings")
        self._accent = accent
        self._build_appearance(accent)
        self._build_sampling(accent)
        self._build_charts(accent)
        self._build_history(accent)
        self._build_notifications(accent)
        self._build_privacy(accent)
        self._build_about(accent)
        self.add_stretch()

    # ------------------------------------------------------------------ helpers

    def _form_card(self, title: str, subtitle: str = "") -> tuple[Card, QFormLayout]:
        """Create a card containing a form layout."""
        card = Card(title, theme=self.theme, subtitle=subtitle, accent=self._accent)
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(self.metrics.space_3)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        card.add(host)
        self.add(card)
        return card, form

    def _bind_combo(
        self, form: QFormLayout, label: str, key: str,
        options: list[tuple[str, object]], hint: str = "",
    ) -> QComboBox:
        """Add a combo box bound to a settings key."""
        box = QComboBox()
        for text, value in options:
            box.addItem(text, value)
        current = self.context.config.get(key)
        index = box.findData(current)
        if index >= 0:
            box.setCurrentIndex(index)
        box.currentIndexChanged.connect(
            lambda: self.context.config.set(key, box.currentData())
        )
        if hint:
            box.setToolTip(hint)
        form.addRow(label, box)
        return box

    def _bind_check(
        self, form: QFormLayout, label: str, key: str, hint: str = ""
    ) -> QCheckBox:
        """Add a checkbox bound to a settings key."""
        box = QCheckBox()
        box.setChecked(bool(self.context.config.get(key, False)))
        box.toggled.connect(lambda value: self.context.config.set(key, value))
        if hint:
            box.setToolTip(hint)
        form.addRow(label, box)
        return box

    def _bind_spin(
        self, form: QFormLayout, label: str, key: str, *,
        minimum: float, maximum: float, step: float = 1.0, suffix: str = "",
        decimals: int = 0, hint: str = "",
    ) -> QWidget:
        """Add a numeric spin box bound to a settings key."""
        if decimals:
            box = QDoubleSpinBox()
            box.setDecimals(decimals)
            box.setSingleStep(step)
        else:
            box = QSpinBox()
            box.setSingleStep(int(step))
        box.setRange(minimum, maximum)  # type: ignore[arg-type]
        box.setSuffix(suffix)
        box.setValue(self.context.config.get(key, minimum))
        box.valueChanged.connect(lambda value: self.context.config.set(key, value))
        if hint:
            box.setToolTip(hint)
        form.addRow(label, box)
        return box

    # ------------------------------------------------------------------ sections

    def _build_appearance(self, accent: str) -> None:
        """Theme and density."""
        card, form = self._form_card(
            "Appearance",
            "Changes apply immediately, with no restart",
        )
        self._bind_combo(form, "Theme", "appearance.theme", [
            ("Dark", "dark"), ("Midnight (OLED)", "midnight"), ("Light", "light"),
        ])
        self._bind_combo(form, "Density", "appearance.density", [
            ("Comfortable", "comfortable"), ("Compact", "compact"),
        ], "Compact reduces padding and row height to fit more on screen.")
        self._bind_spin(
            form, "Text scale", "appearance.font_scale",
            minimum=0.8, maximum=1.6, step=0.05, decimals=2,
            hint="Multiplies every font size in the interface.",
        )
        self._bind_check(form, "Show status bar", "appearance.show_status_bar")
        self._bind_check(
            form, "Enable animations", "appearance.animations",
            "Disable to reduce GPU usage on low-power hardware.",
        )
        note = QLabel(
            "Theme and density changes take effect immediately. Text scale "
            "applies to newly opened pages; reopen the application to restyle "
            "every page."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.palette_tokens.text_subtle};")
        card.add(note)

    def _build_sampling(self, accent: str) -> None:
        """Per-domain polling intervals."""
        card, form = self._form_card(
            "Sampling intervals",
            "Each collector runs on its own cadence — raising an interval "
            "directly reduces CPU use",
        )
        for key, label, hint in (
            ("cpu", "Processor", "Cheap: about 1 ms per sample."),
            ("memory", "Memory", "Cheap: about 1 ms per sample."),
            ("network", "Network", "Cheap: about 6 ms per sample."),
            ("gpu", "Graphics",
             "Moderate: each sample runs nvidia-smi, roughly 25 ms."),
            ("storage", "Storage", "Moderate: about 15 ms per sample."),
            ("sensors", "Sensors", "Moderate: about 15 ms per sample."),
            ("processes", "Processes",
             "Expensive: enumerating several hundred processes costs ~60 ms."),
            ("battery", "Battery", "Cheap, and changes slowly."),
            ("services", "Services",
             "Very expensive (~650 ms). Suspended automatically while the "
             "Services page is closed."),
            ("system", "System inventory",
             "Almost entirely static; a long interval is appropriate."),
        ):
            self._bind_spin(
                form, label, f"sampling.{key}",
                minimum=0.5, maximum=600.0, step=0.5, decimals=1,
                suffix=" s", hint=hint,
            )
        cost = QLabel()
        cost.setWordWrap(True)
        cost.setStyleSheet(f"color: {self.palette_tokens.text_muted};")
        card.add(cost)
        self._cost_label = cost

    def _build_charts(self, accent: str) -> None:
        """Chart appearance and retention."""
        _card, form = self._form_card("Charts")
        self._bind_spin(
            form, "History window", "charts.history_seconds",
            minimum=60, maximum=3600, step=30, suffix=" s",
            hint="How much time the live charts show. Longer windows use more "
                 "memory for the ring buffers.",
        )
        self._bind_spin(
            form, "Line width", "charts.line_width",
            minimum=0.5, maximum=4.0, step=0.1, decimals=1, suffix=" px",
        )
        self._bind_spin(
            form, "Fill opacity", "charts.fill_opacity",
            minimum=0, maximum=120, step=5,
        )
        self._bind_check(form, "Antialiasing", "charts.antialias")
        self._bind_check(form, "Show grid lines", "charts.show_grid")

    def _build_history(self, accent: str) -> None:
        """Persistence settings and database status."""
        card, form = self._form_card(
            "History and storage",
            "Recorded metrics are stored locally in SQLite",
        )
        self._bind_check(
            form, "Record history to disk", "history.enabled",
            "When off, charts still work from memory but nothing is persisted.",
        )
        self._bind_spin(
            form, "Flush interval", "history.flush_seconds",
            minimum=5.0, maximum=120.0, step=5.0, decimals=0, suffix=" s",
            hint="Samples accumulate in memory and are written in one batch.",
        )
        self._bind_spin(
            form, "Retention", "history.retention_days",
            minimum=1, maximum=365, step=1, suffix=" days",
        )
        self._bind_spin(
            form, "Maximum database size", "history.max_megabytes",
            minimum=16, maximum=8192, step=16, suffix=" MB",
            hint="A backstop: older samples are trimmed if the database "
                 "exceeds this even within the retention window.",
        )
        self._storage = KeyValueTable(theme=self.theme, label_width=170)
        for key, label in (
            ("path", "Database location"), ("size", "Current size"),
            ("samples", "Stored samples"), ("status", "Status"),
        ):
            self._storage.add_row(key, label)
        card.add(Divider())
        card.add(self._storage)

        buttons = QHBoxLayout()
        vacuum = QPushButton("Compact database")
        vacuum.clicked.connect(self._vacuum)
        buttons.addWidget(vacuum)
        buttons.addStretch(1)
        card.add_layout(buttons)

    def _build_notifications(self, accent: str) -> None:
        """Alert delivery preferences."""
        card, form = self._form_card("Notifications")
        self._bind_check(
            form, "Desktop notifications", "notifications.desktop_notifications"
        )
        self._bind_check(form, "In-app banners", "notifications.in_app_banners")
        self._bind_spin(
            form, "Repeat cooldown", "notifications.cooldown_seconds",
            minimum=30.0, maximum=3600.0, step=30.0, decimals=0, suffix=" s",
            hint="Minimum time between repeat notifications for the same rule.",
        )
        available = self.context.notifications.desktop_available
        badge = Badge(
            "notify-send available" if available
            else "notify-send not installed — in-app banners only",
            theme=self.theme,
            colour=(
                self.palette_tokens.success if available
                else self.palette_tokens.warning
            ),
        )
        card.add(badge)

        test = QPushButton("Send a test notification")
        test.clicked.connect(self._test_notification)
        card.add(test)

    def _build_privacy(self, accent: str) -> None:
        """Privacy controls and the no-network guarantee."""
        card, form = self._form_card(
            "Privacy",
            "This application has no telemetry, no analytics and no cloud "
            "component",
        )
        self._bind_check(
            form, "Allow latency probes", "privacy.allow_latency_probes",
            "When enabled, the Network page can ping a host you specify. "
            "This is the only feature that contacts a remote machine, and it "
            "is always manually triggered.",
        )
        target = QLineEdit(str(self.context.config.get("privacy.latency_target", "")))
        target.editingFinished.connect(
            lambda: self.context.config.set("privacy.latency_target", target.text())
        )
        form.addRow("Latency probe target", target)
        self._bind_check(
            form, "Redact process command lines", "privacy.redact_command_lines",
            "Hides command-line arguments, which can contain tokens or "
            "file paths, when sharing a screen.",
        )

        statement = QLabel(
            "<b>What this application does not do:</b><br>"
            "• No data is sent anywhere. There is no telemetry, no crash "
            "reporting and no update check.<br>"
            "• Nothing is written outside your own XDG directories.<br>"
            "• Process environment variables that look like credentials are "
            "masked before display.<br>"
            "• The only outbound network access is the latency probe above, "
            "which is off by default and never automatic."
        )
        statement.setWordWrap(True)
        statement.setStyleSheet(
            f"color: {self.palette_tokens.text_muted}; "
            f"background: {self.palette_tokens.surface_sunken}; "
            f"border-radius: {self.metrics.radius_md}px; "
            f"padding: {self.metrics.space_4}px;"
        )
        card.add(statement)

    def _build_about(self, accent: str) -> None:
        """Paths, capability report and reset."""
        card = Card("About and diagnostics", theme=self.theme, accent=accent)
        paths = KeyValueTable(theme=self.theme, label_width=170)
        paths.add_row("config", "Configuration", str(config_dir()))
        paths.add_row("data", "Data", str(data_dir()))
        paths.add_row("state", "State", str(state_dir()))
        paths.add_row("log", "Log file", str(log_file()))
        paths.add_row("settings", "Settings file", str(self.context.config.path))
        card.add(paths)
        card.add(Divider())

        self._capabilities = KeyValueTable(theme=self.theme, columns=2,
                                           label_width=170)
        for name, capability in sorted(
            self.context.capabilities.snapshot().items()
        ):
            label = name.replace("_", " ").title()
            value = "Available" if capability else (capability.detail or "Unavailable")
            self._capabilities.add_row(name, label, value)
        card.add(self._capabilities)
        card.add(Divider())

        buttons = QHBoxLayout()
        export_settings = QPushButton("Export settings…")
        export_settings.clicked.connect(self._export_settings)
        buttons.addWidget(export_settings)

        open_log = QPushButton("Copy log path")
        open_log.clicked.connect(
            lambda: self._copy(str(log_file()))
        )
        buttons.addWidget(open_log)

        reset = QPushButton("Reset all settings")
        reset.setProperty("variant", "danger")
        reset.clicked.connect(self._reset)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        card.add_layout(buttons)
        self.add(card)

    # ------------------------------------------------------------------ actions

    def _vacuum(self) -> None:
        """Compact the history database."""
        database = self.context.history._database
        database.vacuum()
        database.checkpoint()
        self.status_message.emit("Database compacted")
        self._refresh_storage()

    def _test_notification(self) -> None:
        """Send a sample notification so the user can verify delivery."""
        from app.models.diagnostics import Severity

        self.context.notifications.notify(
            "Linux System Observatory",
            "This is a test notification. Alerts will look like this.",
            Severity.INFO,
        )
        self.status_message.emit("Test notification sent")

    def _export_settings(self) -> None:
        """Copy the settings document to a chosen location."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export settings",
            str(export_dir() / "observatory-settings.json"), "JSON (*.json)",
        )
        if not path:
            return
        try:
            import shutil

            self.context.config.save()
            shutil.copy2(self.context.config.path, path)
        except OSError as exc:
            self.status_message.emit(f"Export failed: {exc}")
            return
        self.status_message.emit(f"Settings exported to {path}")

    def _copy(self, value: str) -> None:
        """Copy text to the clipboard."""
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(value)
        self.status_message.emit("Copied to clipboard")

    def _reset(self) -> None:
        """Restore factory defaults after confirmation."""
        answer = QMessageBox.question(
            self, "Reset all settings?",
            "This restores every preference, alert rule and dashboard layout "
            "to its default. Recorded history is not deleted.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.context.config.reset()
        QMessageBox.information(
            self, "Settings reset",
            "Defaults restored. Restart the application for every change to "
            "take effect.",
        )

    def _refresh_storage(self) -> None:
        """Update the database status table."""
        report = self.context.history.storage_report()
        self._storage.update_many({
            "path": report["path"],
            "size": bytes_(report["size_bytes"]),
            "samples": f"{report['samples']:,}",
            "status": (
                "Unavailable" if report["degraded"]
                else "Recording" if report["persisting"]
                else "Recording disabled"
            ),
        })
        total = self.context.monitor.total_cost_ms_per_second
        self._cost_label.setText(
            f"Current total monitoring cost: {total:.0f} ms of CPU per second "
            f"({total / 10:.1f}% of one core). The Diagnostics page breaks this "
            "down per collector."
        )

    def on_shown(self) -> None:
        """Refresh live figures on entry."""
        super().on_shown()
        self._refresh_storage()
