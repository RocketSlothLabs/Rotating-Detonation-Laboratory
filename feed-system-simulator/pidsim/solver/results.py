"""Result containers and CSV export."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..physics.flow import Regime
from ..units import psia_to_psig
from .vessel import VesselTrace


@dataclass
class ComponentFlow:
    """What the solve found for one component."""

    component_id: str
    name: str
    mdot_kgs: float
    regime: Regime
    p_in_psia: float
    p_out_psia: float
    cv: float

    @property
    def delta_p_psi(self) -> float:
        return self.p_in_psia - self.p_out_psia

    @property
    def mdot_gs(self) -> float:
        return self.mdot_kgs * 1e3


@dataclass
class SteadyResult:
    """Steady-state solution for a whole network."""

    node_pressures_psia: dict[str, float]
    component_flows: dict[str, ComponentFlow]
    mode: str
    converged: bool = True
    iterations: int = 0
    residual_norm: float = 0.0
    temperature_R: float = 0.0
    warnings: list[str] = field(default_factory=list)
    solved_boundary_psia: float | None = None
    """Pressure the backward mode solved for, if it ran."""

    def pressure_psig(self, node_id: str) -> float:
        return psia_to_psig(self.node_pressures_psia[node_id])

    def total_mdot_into(self, node_id: str, network) -> float:
        total = 0.0
        for comp in network.components_at(node_id):
            flow = self.component_flows[comp.id]
            if comp.to_node == node_id:
                total += flow.mdot_kgs
            else:
                total -= flow.mdot_kgs
        return total

    def to_csv(self, path: str | Path) -> Path:
        path = Path(path)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["# mode", self.mode])
            writer.writerow(["# converged", self.converged])
            writer.writerow(["# flowing temperature (degR)", self.temperature_R])
            writer.writerow([])
            writer.writerow(["node", "pressure_psia", "pressure_psig"])
            for node_id, p in self.node_pressures_psia.items():
                writer.writerow([node_id, f"{p:.4f}", f"{psia_to_psig(p):.4f}"])
            writer.writerow([])
            writer.writerow(
                [
                    "component",
                    "name",
                    "cv",
                    "mdot_kg_s",
                    "mdot_g_s",
                    "p_in_psia",
                    "p_out_psia",
                    "delta_p_psi",
                    "regime",
                ]
            )
            for flow in self.component_flows.values():
                writer.writerow(
                    [
                        flow.component_id,
                        flow.name,
                        f"{flow.cv:.4f}",
                        f"{flow.mdot_kgs:.6f}",
                        f"{flow.mdot_gs:.3f}",
                        f"{flow.p_in_psia:.4f}",
                        f"{flow.p_out_psia:.4f}",
                        f"{flow.delta_p_psi:.4f}",
                        flow.regime.value,
                    ]
                )
            if self.warnings:
                writer.writerow([])
                writer.writerow(["warnings"])
                for w in self.warnings:
                    writer.writerow([w])
        return path


@dataclass
class TransientResult:
    """Time-domain solution."""

    t: np.ndarray
    node_pressures_psia: dict[str, np.ndarray]
    node_temperatures_K: dict[str, np.ndarray]
    component_mdot_kgs: dict[str, np.ndarray]
    component_names: dict[str, str] = field(default_factory=dict)
    node_names: dict[str, str] = field(default_factory=dict)
    vessel_traces: dict[str, VesselTrace] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def at(self, t_query: float) -> dict[str, dict[str, float]]:
        """Interpolated snapshot of every tracked quantity at one time."""
        return {
            "node_pressure_psia": {
                k: float(np.interp(t_query, self.t, v))
                for k, v in self.node_pressures_psia.items()
            },
            "node_temperature_K": {
                k: float(np.interp(t_query, self.t, v))
                for k, v in self.node_temperatures_K.items()
            },
            "component_mdot_kgs": {
                k: float(np.interp(t_query, self.t, v))
                for k, v in self.component_mdot_kgs.items()
            },
        }

    def to_csv(self, path: str | Path) -> Path:
        path = Path(path)
        node_ids = list(self.node_pressures_psia)
        temp_ids = list(self.node_temperatures_K)
        comp_ids = list(self.component_mdot_kgs)

        header = ["t_s"]
        header += [f"P_psia[{self.node_names.get(n, n)}]" for n in node_ids]
        header += [f"T_K[{self.node_names.get(n, n)}]" for n in temp_ids]
        header += [f"mdot_g_s[{self.component_names.get(c, c)}]" for c in comp_ids]

        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for i, ti in enumerate(self.t):
                row = [f"{ti:.6f}"]
                row += [f"{self.node_pressures_psia[n][i]:.4f}" for n in node_ids]
                row += [f"{self.node_temperatures_K[n][i]:.4f}" for n in temp_ids]
                row += [f"{self.component_mdot_kgs[c][i] * 1e3:.4f}" for c in comp_ids]
                writer.writerow(row)
        return path
