"""Time-domain network solver.

Structure of the integration
---------------------------
Only vessels carry state. At every right-hand-side evaluation the integrator:

1. decodes each vessel's pressure and temperature from the state vector,
2. pins those pressures as boundary conditions and solves the rest of the
   network instantaneously (:func:`pidsim.solver.steady.solve_quasi_steady`),
3. reads each vessel's inflow and outflow off that solution,
4. forms ``dP/dt`` or ``(dm/dt, dU/dt)`` per vessel.

Step 2 is the quasi-steady assumption: pipework volume is negligible next to
vessel volume, so the lines equilibrate far faster than the tanks drain. It is
what lets an arbitrary branching network be integrated with only a couple of
state variables per vessel.

Isolated vs coupled
-------------------
A vessel's formulation is chosen per vessel and never collapsed into one mode
(see :mod:`pidsim.solver.vessel`). ``AUTO`` picks ISENTROPIC only when no
supply path to the vessel could ever open; anything reachable from a source
uses the mass+energy form, which handles inflow correctly and degenerates to
the isentropic answer when the inflow happens to be zero. A valve closing
mid-run therefore switches a branch from coupled to isolated *behaviour*
without switching formulation.

Known v1 limitation
-------------------
Vessel nodes carry their own (cooling) temperature and the Cv equation is
evaluated at the upstream node's temperature. Plain junctions, however, stay
at the project flowing temperature rather than tracking the gas that passes
through them. Gas genuinely cools as it expands down a line; modelling that
would mean carrying a temperature field through the network and is out of
scope for v1. The reference cases discharge a vessel straight into a fixed
manifold, so they are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.integrate import solve_ivp

from ..model.components import Node, NodeKind, VesselModel
from ..model.network import Network
from ..model.project import ProjectSettings
from ..physics import isentropic as isen
from ..units import (
    kelvin_to_rankine,
    pa_to_psi,
    psi_to_pa,
    rankine_to_kelvin,
)
from .results import TransientResult
from .steady import solve_quasi_steady
from .vessel import VesselSpec

_DEFAULT_RTOL = 1e-8
_DEFAULT_ATOL = 1e-10


class TransientError(RuntimeError):
    pass


@dataclass
class _VesselState:
    """Where one vessel's variables live in the global state vector."""

    node_id: str
    name: str
    spec: VesselSpec
    model: VesselModel
    offset: int

    @property
    def size(self) -> int:
        return 1 if self.model is VesselModel.ISENTROPIC else 2

    def decode(self, y: np.ndarray) -> tuple[float, float]:
        """``(pressure psia, temperature K)`` from the state vector."""
        if self.model is VesselModel.ISENTROPIC:
            p = max(float(y[self.offset]), 1e-9)
            t = isen.temperature_at(
                psi_to_pa(p),
                self.spec.charge_pressure_pa,
                self.spec.charge_temperature_K,
                self.spec.gamma,
            )
            return p, t
        m = max(float(y[self.offset]), 1e-12)
        u = float(y[self.offset + 1])
        t = u / (m * self.spec.gas.cv)
        p_pa = isen.ideal_gas_pressure(m, self.spec.volume_m3, self.spec.R, t)
        return pa_to_psi(p_pa), t

    def initial(self) -> list[float]:
        if self.model is VesselModel.ISENTROPIC:
            return [self.spec.charge_pressure_psia]
        return [self.spec.initial_mass, self.spec.initial_energy]


def _vessel_spec(node: Node, settings: ProjectSettings) -> VesselSpec:
    p0 = node.initial_pressure_psia
    if p0 is None:
        raise TransientError(f"Vessel {node.name!r} has no initial pressure")
    if not node.volume_m3:
        raise TransientError(f"Vessel {node.name!r} has no volume")
    return VesselSpec(
        volume_m3=node.volume_m3,
        charge_pressure_psia=p0,
        charge_temperature_R=node.charge_temperature_R,
        gas=settings.gas,
        t_ref_R=settings.t_ref_R,
        name=node.name,
    )


def _can_ever_be_supplied(net: Network, node_id: str) -> bool:
    """Could gas ever reach this vessel from a source?

    Walks outward through every component that is open now or could be opened
    by a schedule. A permanently shut valve severs the path, which is what
    makes a vessel genuinely isolated.
    """
    seen = {node_id}
    queue = [node_id]
    while queue:
        current = queue.pop()
        for comp in net.components_at(current):
            passable = comp.cv > 0.0 and (comp.is_open or comp.schedule is not None)
            if not passable:
                continue
            other = comp.to_node if comp.from_node == current else comp.from_node
            if other in seen:
                continue
            if net.nodes[other].kind is NodeKind.SOURCE:
                return True
            seen.add(other)
            queue.append(other)
    return False


def _resolve_model(net: Network, node: Node) -> VesselModel:
    if node.vessel_model is not VesselModel.AUTO:
        return node.vessel_model
    return (
        VesselModel.ENERGY
        if _can_ever_be_supplied(net, node.id)
        else VesselModel.ISENTROPIC
    )


def solve_transient(
    network: Network,
    settings: ProjectSettings,
    t_span: tuple[float, float],
    n_output: int = 501,
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
    stop_at_ambient: bool = True,
    validate: bool = True,
) -> TransientResult:
    """Integrate the network over ``t_span`` seconds."""
    if validate:
        network.raise_on_errors()

    dynamic = network.dynamic_nodes
    if not dynamic:
        raise TransientError(
            "No component has dynamic state. Add an accumulator, or a "
            "finite-volume source, or use the steady-state solver."
        )

    states: list[_VesselState] = []
    offset = 0
    for node in dynamic:
        model = _resolve_model(network, node)
        state = _VesselState(
            node_id=node.id,
            name=node.name,
            spec=_vessel_spec(node, settings),
            model=model,
            offset=offset,
        )
        states.append(state)
        offset += state.size

    warm: dict[str, float] = {}

    def snapshot(t: float, y: np.ndarray):
        """Vessel states plus the quasi-steady solution of everything else."""
        fixed: dict[str, float] = {}
        temps_R: dict[str, float] = {}
        temps_K: dict[str, float] = {}
        for st in states:
            p, t_k = st.decode(y)
            fixed[st.node_id] = p
            temps_K[st.node_id] = t_k
            temps_R[st.node_id] = kelvin_to_rankine(t_k)
        pressures, flows = solve_quasi_steady(
            network, settings, t, fixed, warm or None, temps_R
        )
        warm.update(pressures)
        return pressures, flows, temps_K

    def vessel_balance(
        state: _VesselState, flows: dict[str, float]
    ) -> tuple[float, float]:
        """``(mdot_in, mdot_out)`` for one vessel, kg/s, both non-negative."""
        mdot_in = 0.0
        mdot_out = 0.0
        for comp in network.components_at(state.node_id):
            signed = flows.get(comp.id, 0.0)
            # Positive `signed` runs from_node -> to_node.
            into = signed if comp.to_node == state.node_id else -signed
            if into >= 0.0:
                mdot_in += into
            else:
                mdot_out += -into
        return mdot_in, mdot_out

    def inflow_temperature_K(
        state: _VesselState, flows: dict[str, float], temps_K: dict[str, float]
    ) -> float:
        """Mass-weighted temperature of everything entering the vessel.

        Normally this is just the supply temperature; the weighting matters
        only when one vessel feeds another.
        """
        total = 0.0
        weighted = 0.0
        for comp in network.components_at(state.node_id):
            signed = flows.get(comp.id, 0.0)
            into = signed if comp.to_node == state.node_id else -signed
            if into <= 0.0:
                continue
            upstream = (
                comp.from_node if comp.to_node == state.node_id else comp.to_node
            )
            t_up = temps_K.get(
                upstream,
                rankine_to_kelvin(
                    network.nodes[upstream].supply_temperature_R
                    if network.nodes[upstream].kind is NodeKind.SOURCE
                    else settings.t_flow_R
                ),
            )
            weighted += into * t_up
            total += into
        if total <= 0.0:
            return rankine_to_kelvin(settings.t_flow_R)
        return weighted / total

    def rhs(t: float, y: np.ndarray) -> np.ndarray:
        _, flows, temps_K = snapshot(t, y)
        dy = np.zeros_like(y)
        for st in states:
            mdot_in, mdot_out = vessel_balance(st, flows)
            if st.model is VesselModel.ISENTROPIC:
                p, _ = st.decode(y)
                net_out = mdot_out - mdot_in
                dy[st.offset] = pa_to_psi(
                    isen.dPdt(net_out, psi_to_pa(p), st.spec.K, st.spec.gamma)
                )
            else:
                t_local = temps_K[st.node_id]
                t_in = inflow_temperature_K(st, flows, temps_K)
                cp = st.spec.gas.cp
                dy[st.offset] = mdot_in - mdot_out
                dy[st.offset + 1] = mdot_in * cp * t_in - mdot_out * cp * t_local
        return dy

    events = []
    if stop_at_ambient:
        floor = settings.ambient_pressure_psia

        for st in states:
            def depleted(t: float, y: np.ndarray, _st=st) -> float:
                return _st.decode(y)[0] - floor

            depleted.terminal = True
            depleted.direction = -1.0
            events.append(depleted)

    y0 = np.array([v for st in states for v in st.initial()], dtype=float)
    sol = solve_ivp(
        rhs,
        t_span,
        y0,
        method="LSODA",
        rtol=rtol,
        atol=atol,
        dense_output=True,
        events=events or None,
    )
    if not sol.success:
        raise TransientError(f"Transient integration failed: {sol.message}")

    t_end = float(sol.t[-1])
    t_out = np.linspace(t_span[0], t_end, n_output)

    node_p: dict[str, np.ndarray] = {n: np.zeros(n_output) for n in network.nodes}
    node_t: dict[str, np.ndarray] = {n: np.zeros(n_output) for n in network.nodes}
    comp_m: dict[str, np.ndarray] = {c: np.zeros(n_output) for c in network.components}

    warm.clear()
    for i, ti in enumerate(t_out):
        y = sol.sol(ti)
        pressures, flows, temps_K = snapshot(ti, y)
        for node_id in network.nodes:
            node_p[node_id][i] = pressures.get(node_id, np.nan)
            node_t[node_id][i] = temps_K.get(
                node_id, rankine_to_kelvin(settings.t_flow_R)
            )
        for comp_id in network.components:
            comp_m[comp_id][i] = flows.get(comp_id, 0.0)

    notes: list[str] = []
    if sol.status == 1:
        notes.append(
            f"Integration stopped at t={t_end:.4f} s: a vessel reached ambient "
            f"pressure ({settings.ambient_pressure_psia:.2f} psia)"
        )
    for st in states:
        notes.append(f"{st.name}: integrated as {st.model.value}")

    return TransientResult(
        t=t_out,
        node_pressures_psia=node_p,
        node_temperatures_K=node_t,
        component_mdot_kgs=comp_m,
        component_names={c.id: c.name for c in network.components.values()},
        node_names={n.id: n.name for n in network.nodes.values()},
        events=notes,
    )
