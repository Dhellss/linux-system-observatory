"""Alerts page: rule management and firing history."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from app.core.units import timestamp
from app.models.diagnostics import AlertRule, Comparison, Severity
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import Badge, EmptyState
from app.ui.widgets.table import Column, DataTable


class AlertsPage(Page):
    """Create, edit and review threshold alerts."""

    domain = ""
    title = "Alerts"
    icon = "⚠"
    section = "Analysis"
    description = "Threshold rules and notification history"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("alerts")
        engine = self.context.alerts

        card = Card(
            "Alert rules", theme=self.theme,
            subtitle="A rule fires only once its condition has held "
                     "continuously for the sustain period",
            accent=accent,
        )
        self._active_badge = Badge("No active alerts", theme=self.theme,
                                   colour=self.palette_tokens.success)
        card.add_action(self._active_badge)

        controls = QHBoxLayout()
        add = QPushButton("Add rule…")
        add.setProperty("variant", "primary")
        add.clicked.connect(self._add_rule)
        controls.addWidget(add)

        edit = QPushButton("Edit…")
        edit.clicked.connect(self._edit_rule)
        controls.addWidget(edit)

        toggle = QPushButton("Enable / disable")
        toggle.clicked.connect(self._toggle_rule)
        controls.addWidget(toggle)

        remove = QPushButton("Delete")
        remove.setProperty("variant", "danger")
        remove.clicked.connect(self._delete_rule)
        controls.addWidget(remove)

        defaults = QPushButton("Restore defaults")
        defaults.clicked.connect(self._restore_defaults)
        controls.addWidget(defaults)
        controls.addStretch(1)
        card.add_layout(controls)

        palette = self.palette_tokens
        columns: list[Column] = [
            Column("state", "", lambda r: "●",
                   width=34,
                   colour=lambda r: (
                       palette.danger if engine.is_firing(r.rule_id)
                       else palette.success if r.enabled
                       else palette.text_subtle
                   )),
            Column("label", "Rule", lambda r: r.label or r.metric, width=230),
            Column("metric", "Metric", lambda r: r.metric, width=190, mono=True),
            Column("condition", "Condition",
                   lambda r: f"{r.comparison.value} {r.threshold:g}",
                   width=110, mono=True),
            Column("sustain", "Sustain", lambda r: r.sustain_seconds,
                   display=lambda v: f"{v:g} s", width=90, mono=True),
            Column("severity", "Severity", lambda r: r.severity.label, width=100,
                   colour=lambda r: palette.status(int(r.severity))),
            Column("current", "Current value",
                   lambda r: engine.last_value(r.rule_id) or 0,
                   display=lambda v: f"{v:.1f}" if v else "—",
                   width=120, mono=True),
            Column("status", "Status",
                   lambda r: (
                       "Firing" if engine.is_firing(r.rule_id)
                       else "Enabled" if r.enabled else "Disabled"
                   ), width=0),
        ]
        self._rules_table = DataTable(columns, theme=self.theme, stretch_column=7)
        self._rules_table.setMinimumHeight(int(self.metrics.row_height * 12))
        self._rules_table.doubleClicked.connect(lambda _: self._edit_rule())
        card.add(self._rules_table)
        self.add(card)

        history = Card("Alert history", theme=self.theme, accent=accent)
        clear = QPushButton("Clear history")
        clear.clicked.connect(self._clear_history)
        history.add_action(clear)

        self._history_table = DataTable(
            [
                Column("time", "When", lambda a: a.occurred,
                       display=lambda v: timestamp(v), width=170, mono=True),
                Column("label", "Rule", lambda a: a.label, width=230),
                Column("transition", "Transition",
                       lambda a: "Triggered" if a.active else "Cleared",
                       width=110,
                       colour=lambda a: (
                           palette.danger if a.active else palette.success
                       )),
                Column("value", "Value", lambda a: a.value,
                       display=lambda v: f"{v:.2f}", width=100, mono=True),
                Column("threshold", "Threshold", lambda a: a.threshold,
                       display=lambda v: f"{v:g}", width=100, mono=True),
                Column("metric", "Metric", lambda a: a.metric, width=0, mono=True),
            ],
            theme=self.theme, stretch_column=5,
        )
        self._history_table.setMinimumHeight(int(self.metrics.row_height * 9))
        history.add(self._history_table)
        self._history_empty = EmptyState(
            "No alerts have fired",
            "Alert transitions are recorded here so you can see what happened "
            "while you were away.",
            theme=self.theme, icon="✓",
        )
        history.add(self._history_empty)
        self.add(history)
        self.add_stretch()

        engine.rules_changed.connect(self._refresh)
        engine.alert_changed.connect(lambda _event: self._refresh())

    def _refresh(self) -> None:
        """Re-render the rules table, history and active badge."""
        engine = self.context.alerts
        self._rules_table.set_rows(engine.rules())
        history = engine.history(300)
        self._history_table.set_rows(history)
        self._history_empty.setVisible(not history)

        active = engine.active()
        palette = self.palette_tokens
        if active:
            worst = engine.worst_severity()
            self._active_badge.set_state(
                f"{len(active)} active", palette.status(int(worst))
            )
        else:
            self._active_badge.set_state("No active alerts", palette.success)

    def _selected(self) -> AlertRule | None:
        """The selected rule, if any."""
        selected = self._rules_table.selected_object()
        return selected if isinstance(selected, AlertRule) else None

    def _add_rule(self) -> None:
        """Open the rule editor for a new rule."""
        dialog = _RuleDialog(self.context, theme=self.theme, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self.context.alerts.add_rule(**values)
        self.status_message.emit(f"Added alert rule for {values['metric']}")

    def _edit_rule(self) -> None:
        """Open the rule editor for the selected rule."""
        rule = self._selected()
        if rule is None:
            self.status_message.emit("Select a rule to edit")
            return
        dialog = _RuleDialog(self.context, theme=self.theme, rule=rule, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        from dataclasses import replace

        self.context.alerts.update_rule(
            replace(rule, **values)
        )
        self.status_message.emit("Rule updated")

    def _toggle_rule(self) -> None:
        """Enable or disable the selected rule."""
        rule = self._selected()
        if rule is None:
            self.status_message.emit("Select a rule first")
            return
        self.context.alerts.set_enabled(rule.rule_id, not rule.enabled)
        self.status_message.emit(
            f"{'Disabled' if rule.enabled else 'Enabled'} "
            f"{rule.label or rule.metric}"
        )

    def _delete_rule(self) -> None:
        """Delete the selected rule after confirmation."""
        rule = self._selected()
        if rule is None:
            self.status_message.emit("Select a rule to delete")
            return
        answer = QMessageBox.question(
            self, "Delete alert rule?",
            f"Delete the rule “{rule.label or rule.metric}”?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.context.alerts.remove_rule(rule.rule_id)
            self.status_message.emit("Rule deleted")

    def _restore_defaults(self) -> None:
        """Replace all rules with the built-in defaults."""
        answer = QMessageBox.question(
            self, "Restore default rules?",
            "This replaces every current rule with the built-in default set.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.context.alerts.restore_defaults()
            self.status_message.emit("Default alert rules restored")

    def _clear_history(self) -> None:
        """Delete recorded alert transitions."""
        self.context.alerts.clear_history()
        self._refresh()
        self.status_message.emit("Alert history cleared")

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self._refresh()

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Keep current values live while the page is open."""
        if self.is_visible_page:
            self._rules_table.set_rows(self.context.alerts.rules())


class _RuleDialog(QDialog):
    """Editor for a single alert rule."""

    def __init__(self, context, *, theme, rule: AlertRule | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit alert rule" if rule else "New alert rule")
        self.setMinimumWidth(460)
        self._registry = context.metrics

        form = QFormLayout(self)
        self._metric = QComboBox()
        for _domain, definitions in self._registry.grouped():
            for definition in definitions:
                self._metric.addItem(
                    f"{definition.label}  ({definition.name})", definition.name
                )
        form.addRow("Metric", self._metric)

        self._label = QLineEdit()
        self._label.setPlaceholderText("Shown in notifications")
        form.addRow("Label", self._label)

        self._comparison = QComboBox()
        self._comparison.addItem("Rises above", Comparison.ABOVE)
        self._comparison.addItem("Falls below", Comparison.BELOW)
        form.addRow("Condition", self._comparison)

        self._threshold = QDoubleSpinBox()
        self._threshold.setRange(-1e12, 1e12)
        self._threshold.setDecimals(2)
        form.addRow("Threshold", self._threshold)

        self._sustain = QDoubleSpinBox()
        self._sustain.setRange(0.0, 3600.0)
        self._sustain.setSuffix(" s")
        self._sustain.setValue(10.0)
        self._sustain.setToolTip(
            "How long the condition must hold before the alert fires. "
            "Raising this suppresses momentary spikes."
        )
        form.addRow("Sustain for", self._sustain)

        self._severity = QComboBox()
        for level in (Severity.INFO, Severity.WARNING, Severity.CRITICAL):
            self._severity.addItem(level.label, level)
        self._severity.setCurrentIndex(1)
        form.addRow("Severity", self._severity)

        self._notify = QComboBox()
        self._notify.addItem("Yes", True)
        self._notify.addItem("No", False)
        form.addRow("Send notification", self._notify)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._metric.currentIndexChanged.connect(self._prefill_from_metric)
        if rule is not None:
            self._load(rule)
        else:
            self._prefill_from_metric()

    def _load(self, rule: AlertRule) -> None:
        """Populate the form from an existing rule."""
        index = self._metric.findData(rule.metric)
        if index >= 0:
            self._metric.setCurrentIndex(index)
        self._label.setText(rule.label)
        self._comparison.setCurrentIndex(
            0 if rule.comparison is Comparison.ABOVE else 1
        )
        self._threshold.setValue(rule.threshold)
        self._sustain.setValue(rule.sustain_seconds)
        self._severity.setCurrentIndex(
            {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
            .get(rule.severity, 1)
        )
        self._notify.setCurrentIndex(0 if rule.notify else 1)

    def _prefill_from_metric(self) -> None:
        """Suggest a sensible threshold and direction for the chosen metric.

        The registry knows each metric's typical danger point and whether a low
        value is the problem, so the dialog opens with a defensible default
        rather than zero.
        """
        name = self._metric.currentData()
        definition = self._registry.get(name)
        if definition is None:
            return
        if not self._label.text():
            self._label.setPlaceholderText(definition.label)
        self._comparison.setCurrentIndex(1 if definition.inverted else 0)
        if definition.suggested_threshold:
            self._threshold.setValue(definition.suggested_threshold)

    def values(self) -> dict:
        """The edited rule fields."""
        return {
            "metric": self._metric.currentData(),
            "comparison": self._comparison.currentData(),
            "threshold": self._threshold.value(),
            "severity": self._severity.currentData(),
            "label": self._label.text().strip()
            or self._label.placeholderText(),
            "sustain_seconds": self._sustain.value(),
            "notify": self._notify.currentData(),
        }
