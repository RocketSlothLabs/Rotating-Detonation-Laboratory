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

Node temperatures
-----------------
Vessels carry their own (cooling) temperature as ODE state. Sources sit at
their own ``supply_temperature_R``, fixed. Everything else -- junctions,
tees, the sink -- has no thermal state of its own, so its temperature is just
the mass-weighted average of whatever is flowing into it *this instant*
(:func:`_propagate_node_temperatures`): the same calculation the vessel
energy balance already needed for its own inflow enthalpy term, generalised
to every node. A junction downstream of a cooling accumulator therefore
reports, and feeds into, the gas temperature actually arriving there, not a
flat project default.

That calculation needs this instant's flow solution, which needs node
temperatures to evaluate the Cv equation -- so each junction's temperature
necessarily lags the flow solve that used it by one evaluation, seeded from
the previous call via ``junction_temp_cache``. In practice this lag is a
solver sub-step, not a user-visible time step, and the Cv equation's
dependence on temperature is weak (``Q ~ 1/sqrt(T)``), so a single pass per
evaluation is accurate enough without a nested re-solve.

What this does *not* model: gas cooling continuously *along* a line's own
length between its two endpoints. Lines carry no volume or thermal mass here
(see the quasi-steady assumption above), so temperature only changes in
discrete steps at each node, never gradually along a run.
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


def _propagate_node_temperatures(
    network: Network,
    flows: dict[str, float],
    known_K: dict[str, float],
    t_flow_R: float,
) -> dict[str, float]:
    """Spread known temperatures (sources, vessels) through a flow solution.

    A node becomes resolvable once every component actually flowing *into* it
    (by the sign of ``flows`` this instant) traces back to an already-known
    node; its temperature is then the mass-weighted average of those inflows.
    Worked as a worklist rather than a single rooted walk because the network
    can have more than one source and because a branch that splits and merges
    back means a node's inflows don't all trace to the same upstream path.

    A node with no positive inflow at all -- a dead leg, or nothing has
    reached it yet -- resolves immediately to ``t_flow_R``, so it can never
    block another node's resolution. Any node that somehow never resolves
    (only possible if the flow solution itself contains a genuine cycle,
    which the model's topology rules out) gets the same fallback rather than
    looping forever.
    """
    temps = dict(known_K)
    pending = set(network.nodes) - set(temps)
    progressed = True
    while pending and progressed:
        progressed = False
        for node_id in list(pending):
            total = 0.0
            weighted = 0.0
            ready = True
            for comp in network.components_at(node_id):
                signed = flows.get(comp.id, 0.0)
                into = signed if comp.to_node == node_id else -signed
                if into <= 0.0:
                    continue
                upstream = (
                    comp.from_node if comp.to_node == node_id else comp.to_node
                )
                if upstream not in temps:
                    ready = False
                    break
                weighted += into * temps[upstream]
                total += into
            if not ready:
                continue
            temps[node_id] = (
                weighted / total if total > 0.0 else rankine_to_kelvin(t_flow_R)
            )
            pending.discard(node_id)
            progressed = True

    fallback = rankine_to_kelvin(t_flow_R)
    for node_id in pending:
        temps[node_id] = fallback
    return temps


_DEFAULT_EQUILIBRIUM_RTOL = 1e-4


def solve_transient(
    network: Network,
    settings: ProjectSettings,
    t_span: tuple[float, float],
    n_output: int = 501,
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
    stop_at_ambient: bool = True,
    stop_at_equilibrium: bool = True,
    equilibrium_rtol: float = _DEFAULT_EQUILIBRIUM_RTOL,
    validate: bool = True,
) -> TransientResult:
    """Integrate the network over ``t_span`` seconds.

    ``stop_at_equilibrium`` adds a terminal event that ends the run once
    every vessel's net mass flow (the thing that actually drives its own
    dP/dt or dm/dt -- not gross flow, so a *coupled* vessel correctly keeps
    running until its throughput itself stops changing, not until flow
    stops) has decayed to ``equilibrium_rtol`` of its own peak magnitude for
    the run. Without this, a vessel approaching its backpressure needs
    solve_ivp to resolve an ever-flatter asymptotic tail under the solver's
    tight ``rtol``, which can cost tens of thousands of extra integration
    steps to add essentially nothing to the reported pressure curve -- see
    DEVELOPMENT-NOTES.md section 16 for the numbers that motivated this.

    Skipped automatically whenever any component has a valve schedule with a
    future open/close time: terminating the instant flow looks small would
    risk mistaking a scheduled valve's temporary lull for true equilibrium
    and cutting off real dynamics that were about to resume.
    """
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
    junction_temp_cache: dict[str, float] = {}

    def snapshot(t: float, y: np.ndarray):
        """Vessel states plus the quasi-steady solution of everything else."""
        fixed: dict[str, float] = {}
        # Seed from last call's propagated junction temperatures (the one-
        # evaluation lag described in the module docstring); vessel temps
        # below always override since they're authoritative for this instant.
        temps_R: dict[str, float] = dict(junction_temp_cache)
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

        known = dict(temps_K)
        for node in network.nodes.values():
            if node.kind is NodeKind.SOURCE and node.id not in known:
                known[node.id] = rankine_to_kelvin(node.supply_temperature_R)
        temps_K = _propagate_node_temperatures(
            network, flows, known, settings.t_flow_R
        )
        junction_temp_cache.clear()
        junction_temp_cache.update(
            {nid: kelvin_to_rankine(t) for nid, t in temps_K.items()}
        )
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
        only when one vessel feeds another. ``temps_K`` is always complete by
        the time this runs -- snapshot() has already propagated it to every
        node, sources included -- so each upstream lookup is a direct hit,
        never a fallback.
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
            weighted += into * temps_K[upstream]
            total += into
        if total <= 0.0:
            return rankine_to_kelvin(settings.t_flow_R)
        return weighted / total

    peak_net_mdot = {st.node_id: 1e-12 for st in states}  # tiny floor, never 0

    def rhs(t: float, y: np.ndarray) -> np.ndarray:
        _, flows, temps_K = snapshot(t, y)
        dy = np.zeros_like(y)
        for st in states:
            mdot_in, mdot_out = vessel_balance(st, flows)
            net_out = mdot_out - mdot_in
            peak_net_mdot[st.node_id] = max(peak_net_mdot[st.node_id], abs(net_out))
            if st.model is VesselModel.ISENTROPIC:
                p, _ = st.decode(y)
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

    def equilibrium_margin(t: float, y: np.ndarray) -> float:
        """Positive while any vessel is still meaningfully changing; crosses
        to negative, terminally, only once *every* vessel's net flow has
        decayed below its own threshold -- a coupled vessel's gross
        throughput can stay large forever, so this checks the net flow that
        actually drives its state, same quantity rhs() already uses."""
        _, flows, _ = snapshot(t, y)
        margins = []
        for st in states:
            mdot_in, mdot_out = vessel_balance(st, flows)
            net_now = abs(mdot_out - mdot_in)
            threshold = equilibrium_rtol * peak_net_mdot[st.node_id]
            margins.append(net_now - threshold)
        return max(margins)

    events = []
    if stop_at_ambient:
        floor = settings.ambient_pressure_psia

        for st in states:
            def depleted(t: float, y: np.ndarray, _st=st) -> float:
                return _st.decode(y)[0] - floor

            depleted.terminal = True
            depleted.direction = -1.0
            events.append(depleted)

    has_pending_schedule = any(
        comp.schedule is not None
        and (comp.schedule.open_time is not None or comp.schedule.close_time is not None)
        for comp in network.components.values()
    )
    equilibrium_event_index: int | None = None
    if stop_at_equilibrium and not has_pending_schedule:
        equilibrium_margin.terminal = True
        equilibrium_margin.direction = -1.0
        equilibrium_event_index = len(events)
        events.append(equilibrium_margin)

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
    junction_temp_cache.clear()
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
        hit_equilibrium = (
            equilibrium_event_index is not None
            and sol.t_events[equilibrium_event_index].size > 0
        )
        if hit_equilibrium:
            notes.append(
                f"Integration stopped at t={t_end:.4f} s: net flow into every "
                f"vessel had decayed to within {equilibrium_rtol:g} of its peak "
                "for this run -- effectively at steady state"
            )
        else:
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
