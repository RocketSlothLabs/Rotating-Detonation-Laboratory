"""Matplotlib window for transient time-series results."""

from __future__ import annotations

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import (
    NavigationToolbar2QT as NavigationToolbar,
)
from matplotlib.figure import Figure
from PySide6.QtWidgets import (
    QDialog,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..solver.results import TransientResult
from ..units import psia_to_psig

# Colourblind-safe qualitative sequence, dark enough to read on white.
SERIES_COLOURS = [
    "#1f6feb",
    "#d1242f",
    "#1a7f37",
    "#9a6700",
    "#8250df",
    "#0f6b70",
    "#bc4c00",
]


class _PlotTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.figure = Figure(figsize=(7, 4.2), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(NavigationToolbar(self.canvas, self))
        layout.addWidget(self.canvas)

    def axes(self):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.grid(True, alpha=0.25, linewidth=0.7)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        return ax


class TransientPlotWindow(QDialog):
    """Pressure, temperature and mass-flow histories from a transient run."""

    def __init__(self, result: TransientResult, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transient results")
        self.resize(900, 600)
        self.result = result

        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        self._plot_pressure()
        self._plot_temperature()
        self._plot_mass_flow()

    def _add_tab(self, title: str) -> _PlotTab:
        tab = _PlotTab()
        self.tabs.addTab(tab, title)
        return tab

    def _plot_pressure(self) -> None:
        tab = self._add_tab("Pressure")
        ax = tab.axes()
        for i, (node_id, series) in enumerate(
            self.result.node_pressures_psia.items()
        ):
            label = self.result.node_names.get(node_id, node_id)
            ax.plot(
                self.result.t,
                [psia_to_psig(p) for p in series],
                label=label,
                color=SERIES_COLOURS[i % len(SERIES_COLOURS)],
                linewidth=1.8,
            )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Pressure (psig)")
        ax.legend(frameon=False, fontsize=9)
        tab.canvas.draw_idle()

    def _plot_temperature(self) -> None:
        tab = self._add_tab("Temperature")
        ax = tab.axes()
        for i, (node_id, series) in enumerate(
            self.result.node_temperatures_K.items()
        ):
            label = self.result.node_names.get(node_id, node_id)
            ax.plot(
                self.result.t,
                series,
                label=label,
                color=SERIES_COLOURS[i % len(SERIES_COLOURS)],
                linewidth=1.8,
            )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Temperature (K)")
        ax.legend(frameon=False, fontsize=9)
        tab.canvas.draw_idle()

    def _plot_mass_flow(self) -> None:
        tab = self._add_tab("Mass flow")
        ax = tab.axes()
        for i, (comp_id, series) in enumerate(
            self.result.component_mdot_kgs.items()
        ):
            label = self.result.component_names.get(comp_id, comp_id)
            ax.plot(
                self.result.t,
                [m * 1e3 for m in series],
                label=label,
                color=SERIES_COLOURS[i % len(SERIES_COLOURS)],
                linewidth=1.8,
            )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Mass flow (g/s)")
        ax.legend(frameon=False, fontsize=9)
        tab.canvas.draw_idle()

    def save_png(self, path: str) -> None:
        self.tabs.currentWidget().figure.savefig(path, dpi=200)
