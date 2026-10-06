"""Main window.

Wires the canvas, palette and property editor to the solvers. This module
contains no physics: every number shown comes from :mod:`pidsim.solver`.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QImage, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..model.network import NetworkError, Severity
from ..model.project import Project, ProjectSettings, load, save
from ..physics.gases import available_gases
from ..solver.steady import SolveError, SolveMode, solve_steady
from ..solver.transient import TransientError, solve_transient
from ..units import psia_to_psig
from .canvas import PidScene, PidView
from .examples import EXAMPLES
from .palette import PalettePanel
from .plots import TransientPlotWindow
from .properties import PropertyPanel


class ProjectSettingsDialog(QDialog):
    """Working gas and the two temperatures the Cv model depends on."""

    def __init__(self, settings: ProjectSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project settings")
        self.settings = settings

        form = QFormLayout()
        self.gas = QComboBox()
        for name in available_gases():
            self.gas.addItem(name)
        index = self.gas.findText(settings.gas_name)
        if index >= 0:
            self.gas.setCurrentIndex(index)
        form.addRow("Working gas", self.gas)

        self.t_flow = QDoubleSpinBox()
        self.t_flow.setRange(1.0, 5000.0)
        self.t_flow.setDecimals(2)
        self.t_flow.setSuffix(" degR")
        self.t_flow.setValue(settings.t_flow_R)
        form.addRow("Flowing temperature", self.t_flow)

        self.t_ref = QDoubleSpinBox()
        self.t_ref.setRange(1.0, 5000.0)
        self.t_ref.setDecimals(2)
        self.t_ref.setSuffix(" degR")
        self.t_ref.setValue(settings.t_ref_R)
        form.addRow("SCFH reference temperature", self.t_ref)

        note = QLabel(
            "530 degR (70 degF) is the reference state the validated cases use. "
            "The flowing temperature enters the Cv equation; the SCFH reference "
            "temperature only converts standard volume to mass and should be "
            "left fixed unless you know you want it otherwise."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7383; font-size: 10px;")
        form.addRow(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def apply(self) -> None:
        self.settings.gas_name = self.gas.currentText()
        self.settings.t_flow_R = self.t_flow.value()
        self.settings.t_ref_R = self.t_ref.value()


class TransientDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run transient")

        form = QFormLayout()
        self.duration = QDoubleSpinBox()
        self.duration.setRange(1e-4, 1e5)
        self.duration.setDecimals(4)
        self.duration.setSuffix(" s")
        self.duration.setValue(1.0)
        form.addRow("Duration", self.duration)

        self.samples = QSpinBox()
        self.samples.setRange(21, 20001)
        self.samples.setValue(501)
        form.addRow("Output samples", self.samples)

        self.annotate_at = QDoubleSpinBox()
        self.annotate_at.setRange(0.0, 1e5)
        self.annotate_at.setDecimals(4)
        self.annotate_at.setSuffix(" s")
        self.annotate_at.setValue(1.0)
        form.addRow("Annotate canvas at", self.annotate_at)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)


class RegulatorDialog(QDialog):
    """Pick which regulator the backward solve should size."""

    def __init__(self, regulators, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Size a regulator")
        self.combo = QComboBox()
        for comp in regulators:
            self.combo.addItem(comp.name, comp.id)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Solve for the outlet pressure of:"))
        layout.addWidget(self.combo)
        layout.addWidget(buttons)

    def selected_id(self) -> str:
        return self.combo.currentData()


class MainWindow(QMainWindow):
    def __init__(self, project: Project | None = None):
        super().__init__()
        self.setWindowTitle("P&ID Flow Simulator")
        self.resize(1440, 900)

        self.project = project or next(iter(EXAMPLES.values()))()
        self.current_path: Path | None = None
        self.last_transient = None
        self._plot_window: TransientPlotWindow | None = None

        self.scene = PidScene(self.project.network)
        self.view = PidView(self.scene)
        self.setCentralWidget(self.view)

        self._build_docks()
        self._build_actions()

        self.scene.selection_changed.connect(self.properties.show_object)
        self.scene.status_message.connect(self.statusBar().showMessage)
        self.scene.network_changed.connect(self._on_network_changed)
        self.properties.changed.connect(self._on_property_changed)
        self.palette_panel.part_selected.connect(
            lambda part: setattr(self.scene, "pending_part", part)
        )

        self.scene.rebuild(self.project.network)
        self.scene.pending_part = self.palette_panel.current_part()
        self._log(f"Loaded example: {self.project.name}")
        if self.project.notes:
            self._log(self.project.notes)
        self._validate_quietly()

    # --- construction ------------------------------------------------------

    def _build_docks(self) -> None:
        self.palette_panel = PalettePanel()
        dock = QDockWidget("Palette", self)
        dock.setWidget(self.palette_panel)
        dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        self.properties = PropertyPanel()
        dock = QDockWidget("Properties", self)
        dock.setWidget(self.properties)
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        dock = QDockWidget("Results", self)
        dock.setWidget(self.log)
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [220], Qt.Orientation.Vertical)

    def _act(self, text: str, slot, shortcut: str | None = None, tip: str = ""):
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setStatusTip(tip)
        return action

    def _build_actions(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self._act("&New", self.new_design, "Ctrl+N"))
        file_menu.addAction(self._act("&Open...", self.open_design, "Ctrl+O"))
        file_menu.addAction(self._act("&Save", self.save_design, "Ctrl+S"))
        file_menu.addAction(
            self._act("Save &As...", self.save_design_as, "Ctrl+Shift+S")
        )
        file_menu.addSeparator()
        examples = file_menu.addMenu("Load &example")
        for name, factory in EXAMPLES.items():
            examples.addAction(
                self._act(name, lambda _=False, f=factory: self.load_example(f))
            )
        file_menu.addSeparator()
        file_menu.addAction(
            self._act("Export results to &CSV...", self.export_csv)
        )
        file_menu.addAction(self._act("Export diagram to &PNG...", self.export_png))
        file_menu.addSeparator()
        file_menu.addAction(self._act("E&xit", self.close, "Ctrl+Q"))

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(
            self._act("&Delete selection", self.scene.delete_selection, "Del")
        )
        edit_menu.addSeparator()
        edit_menu.addAction(
            self._act(
                "&Rotate 90\N{DEGREE SIGN} clockwise",
                lambda: self.scene.rotate_selection(90.0),
                "R",
                "Turn the selected nodes; layout only, results are unaffected",
            )
        )
        edit_menu.addAction(
            self._act(
                "Rotate 90\N{DEGREE SIGN} &counter-clockwise",
                lambda: self.scene.rotate_selection(-90.0),
                "Shift+R",
            )
        )
        edit_menu.addSeparator()
        edit_menu.addAction(
            self._act("&Project settings...", self.edit_settings, "Ctrl+,")
        )

        run_menu = self.menuBar().addMenu("&Run")
        run_menu.addAction(
            self._act(
                "Steady -- &forward",
                self.run_forward,
                "F5",
                "Supply pressure known; find what reaches the engine",
            )
        )
        run_menu.addAction(
            self._act(
                "Steady -- &backward",
                self.run_backward,
                "F6",
                "Engine conditions known; find the supply pressure required",
            )
        )
        run_menu.addAction(
            self._act("Size a &regulator...", self.run_size_regulator, "F7")
        )
        run_menu.addSeparator()
        run_menu.addAction(self._act("&Transient...", self.run_transient, "F8"))
        run_menu.addSeparator()
        run_menu.addAction(self._act("&Validate network", self.validate_network))
        run_menu.addAction(self._act("&Clear annotations", self.scene.clear_results))

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self._act("&Fit to diagram", self.fit_view, "Ctrl+0"))
        view_menu.addAction(self._act("&Reset zoom", self.view.reset_zoom))

        toolbar = self.addToolBar("Run")
        toolbar.setMovable(False)
        toolbar.addAction(self._act("Forward", self.run_forward))
        toolbar.addAction(self._act("Backward", self.run_backward))
        toolbar.addAction(self._act("Transient", self.run_transient))
        toolbar.addSeparator()
        toolbar.addAction(self._act("Validate", self.validate_network))
        toolbar.addAction(self._act("Clear", self.scene.clear_results))
        toolbar.addSeparator()
        toolbar.addAction(
            self._act("Rotate", lambda: self.scene.rotate_selection(90.0))
        )
        toolbar.addAction(self._act("Fit", self.fit_view))

    # --- helpers -----------------------------------------------------------

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum()
        )

    def _rule(self, title: str) -> None:
        self._log("")
        self._log(f"=== {title} ===")

    def _on_network_changed(self) -> None:
        self.scene.clear_results()

    def _on_property_changed(self, obj_id: str) -> None:
        self.scene.refresh_item(obj_id)
        self.scene.clear_results()

    def _validate_quietly(self) -> None:
        issues = self.project.network.validate()
        errors = [i for i in issues if i.severity is Severity.ERROR]
        if errors:
            self.statusBar().showMessage(
                f"{len(errors)} problem(s) -- run Validate network for detail"
            )
        else:
            self.statusBar().showMessage("Network is valid")

    def fit_view(self) -> None:
        rect = self.scene.itemsBoundingRect()
        if rect.isEmpty():
            rect = QRectF(-400, -200, 800, 400)
        self.view.fitInView(rect.adjusted(-60, -60, 60, 60), Qt.AspectRatioMode.KeepAspectRatio)

    # --- file --------------------------------------------------------------

    def _set_project(self, project: Project, path: Path | None) -> None:
        self.project = project
        self.current_path = path
        self.scene.rebuild(project.network)
        self.properties.show_object(None)
        self.last_transient = None
        title = path.name if path else project.name
        self.setWindowTitle(f"P&ID Flow Simulator -- {title}")
        self.fit_view()
        self._validate_quietly()

    def new_design(self) -> None:
        from ..model.network import Network

        self._set_project(
            Project(settings=ProjectSettings(), network=Network(), name="Untitled"),
            None,
        )
        self._log("New empty design")

    def load_example(self, factory) -> None:
        project = factory()
        self._set_project(project, None)
        self._rule(f"Example: {project.name}")
        if project.notes:
            self._log(project.notes)

    def open_design(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open design", "", "P&ID design (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            project = load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not open", str(exc))
            return
        self._set_project(project, Path(path))
        self._log(f"Opened {path}")

    def save_design(self) -> None:
        if self.current_path is None:
            self.save_design_as()
            return
        save(self.project, self.current_path)
        self._log(f"Saved {self.current_path}")
        self.statusBar().showMessage(f"Saved {self.current_path.name}")

    def save_design_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save design", "design.json", "P&ID design (*.json)"
        )
        if not path:
            return
        self.current_path = Path(path)
        save(self.project, self.current_path)
        self.setWindowTitle(f"P&ID Flow Simulator -- {self.current_path.name}")
        self._log(f"Saved {path}")

    def export_csv(self) -> None:
        result = getattr(self, "last_result", None)
        if self.last_transient is None and result is None:
            QMessageBox.information(self, "Nothing to export", "Run a solve first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results", "results.csv", "CSV (*.csv)"
        )
        if not path:
            return
        target = self.last_transient if self.last_transient is not None else result
        target.to_csv(path)
        self._log(f"Exported results to {path}")

    def export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export diagram", "diagram.png", "PNG image (*.png)"
        )
        if not path:
            return
        rect = self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        scale = 2.0
        image = QImage(
            int(rect.width() * scale),
            int(rect.height() * scale),
            QImage.Format.Format_ARGB32,
        )
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.scene.render(painter, target=QRectF(image.rect()), source=rect)
        painter.end()
        image.save(path)
        self._log(f"Exported diagram to {path}")

    def edit_settings(self) -> None:
        dialog = ProjectSettingsDialog(self.project.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply()
            self.scene.clear_results()
            self._log(
                f"Settings: {self.project.settings.gas_name}, "
                f"T_flow={self.project.settings.t_flow_R:g} degR, "
                f"T_ref={self.project.settings.t_ref_R:g} degR"
            )

    # --- solving -----------------------------------------------------------

    def validate_network(self) -> None:
        self._rule("Validation")
        issues = self.project.network.validate()
        if not issues:
            self._log("No problems found.")
        for issue in issues:
            self._log(str(issue))
        self._validate_quietly()

    def _run_steady(self, mode: SolveMode, solve_for: str | None = None) -> None:
        try:
            result = solve_steady(
                self.project.network, self.project.settings, mode, solve_for
            )
        except (NetworkError, SolveError) as exc:
            self._rule(f"Steady ({mode.value}) failed")
            self._log(str(exc))
            QMessageBox.warning(self, "Cannot solve", str(exc))
            return
        except Exception:
            self._rule("Unexpected solver error")
            self._log(traceback.format_exc())
            return

        self.last_result = result
        self.last_transient = None
        self.scene.show_steady_result(result)

        self._rule(f"Steady state -- {mode.value}")
        self._log(
            f"Gas {self.project.settings.gas_name}, "
            f"T_flow {self.project.settings.t_flow_R:g} degR"
        )
        if result.solved_boundary_psia is not None:
            self._log(
                f"Required pressure: {result.solved_boundary_psia:.2f} psia "
                f"({psia_to_psig(result.solved_boundary_psia):.2f} psig)"
            )
        self._log("")
        self._log(f"{'Node':<22}{'psia':>12}{'psig':>12}")
        for node_id, p in result.node_pressures_psia.items():
            name = self.project.network.nodes[node_id].name
            self._log(f"{name[:21]:<22}{p:>12.2f}{psia_to_psig(p):>12.2f}")

        self._log("")
        self._log(f"{'Component':<26}{'Cv':>8}{'g/s':>10}{'dP psi':>10}  regime")
        for flow in result.component_flows.values():
            self._log(
                f"{flow.name[:25]:<26}{flow.cv:>8.3f}{flow.mdot_gs:>10.2f}"
                f"{flow.delta_p_psi:>10.2f}  {flow.regime.value}"
            )

        for warning in result.warnings:
            self._log(f"! {warning}")
        if not result.converged:
            self.statusBar().showMessage("Solve did not converge -- see Results")
        else:
            self.statusBar().showMessage("Steady solve complete")

    def run_forward(self) -> None:
        self._run_steady(SolveMode.FORWARD)

    def run_backward(self) -> None:
        self._run_steady(SolveMode.BACKWARD)

    def run_size_regulator(self) -> None:
        regulators = [
            c for c in self.project.network.components.values() if c.is_regulator
        ]
        if not regulators:
            QMessageBox.information(
                self,
                "No regulator",
                "Add a component of type Regulator to size one.",
            )
            return
        dialog = RegulatorDialog(regulators, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_steady(SolveMode.BACKWARD, dialog.selected_id())

    def run_transient(self) -> None:
        dialog = TransientDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        duration = dialog.duration.value()
        try:
            result = solve_transient(
                self.project.network,
                self.project.settings,
                (0.0, duration),
                n_output=dialog.samples.value(),
            )
        except (NetworkError, TransientError) as exc:
            self._rule("Transient failed")
            self._log(str(exc))
            QMessageBox.warning(self, "Cannot solve", str(exc))
            return
        except Exception:
            self._rule("Unexpected solver error")
            self._log(traceback.format_exc())
            return

        self.last_transient = result
        annotate_at = min(dialog.annotate_at.value(), float(result.t[-1]))
        self.scene.show_transient_snapshot(result, annotate_at)

        self._rule(f"Transient -- 0 to {result.t[-1]:g} s")
        for event in result.events:
            self._log(event)
        self._log("")
        self._log(f"State at t = {annotate_at:g} s")
        snapshot = result.at(annotate_at)
        for node_id, p in snapshot["node_pressure_psia"].items():
            name = self.project.network.nodes[node_id].name
            temp = snapshot["node_temperature_K"][node_id]
            self._log(
                f"  {name[:24]:<26}{psia_to_psig(p):>10.2f} psig{temp:>10.2f} K"
            )
        for comp_id, mdot in snapshot["component_mdot_kgs"].items():
            name = self.project.network.components[comp_id].name
            self._log(f"  {name[:24]:<26}{mdot * 1e3:>10.2f} g/s")

        self._plot_window = TransientPlotWindow(result, self)
        self._plot_window.show()
        self.statusBar().showMessage("Transient complete")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("P&ID Flow Simulator")

    project = None
    if len(argv) > 1 and argv[1].endswith(".json"):
        project = load(argv[1])

    window = MainWindow(project)
    window.show()
    window.fit_view()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
