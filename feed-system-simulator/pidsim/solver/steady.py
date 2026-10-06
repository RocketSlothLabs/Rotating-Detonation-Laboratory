"""Steady-state nodal network solver.

Formulation
-----------
One unknown pressure per node that has no pressure boundary, one mass-balance
equation per unknown node, solved simultaneously. Splits, merges and parallel
paths all fall out of the same residual -- flow divides according to each
branch's own pressure/flow relationship, never evenly by assumption.

Which nodes contribute what:

===================  ==================  ==========================
Node                 Pressure unknown?   Contributes an equation?
===================  ==================  ==========================
Internal junction    yes                 yes (net mass flow = 0)
Source (supply)      no (fixed)          no -- it supplies whatever is drawn
Sink, P specified    no (fixed)          only if mdot is ALSO specified
Sink, mdot only      yes                 yes (net inflow = target mdot)
Backward boundary    yes (the answer)    n/a
===================  ==================  ==========================

That bookkeeping stays square in every mode:

* **Forward** -- supply pressure known, engine pressure known: solve the
  interior, report what the engine receives.
* **Forward, mdot-specified engine** -- the engine's pressure joins the
  unknowns and its draw supplies the extra equation.
* **Backward** -- engine pressure *and* mdot both pinned, which
  over-determines the downstream end; the supply pressure becomes the unknown
  that absorbs it. This is the sizing direction that produces the validated
  reference cases.

Unknowns are carried as ``ln(P)`` so the solver cannot step to a negative
absolute pressure, which the square roots in the Cv equation would not survive.

Temperature: steady-state runs are isothermal at the project's flowing
temperature. Real expansion across a restriction cools the gas, but the
hand-validated reference model uses a single flowing temperature throughout,
so this solver reproduces that. Temperature only becomes a state variable in
transient runs, where vessel cooling genuinely drives the answer.
"""

from __future__ import annotations

import math
from enum import Enum

import numpy as np
from scipy.optimize import least_squares, root

from ..model.components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
)
from ..model.network import Network
from ..model.project import ProjectSettings
from ..physics.flow import (
    Regime,
    mass_flow,
    mass_flow_with_regime,
    p1_for_mass_flow,
    signed_mass_flow,
)
from ..units import psia_to_psig, psig_to_psia
from .results import ComponentFlow, SteadyResult


class SolveMode(str, Enum):
    FORWARD = "forward"
    """Supply pressure given; find what reaches the engine."""

    BACKWARD = "backward"
    """Engine conditions given; find the supply pressure they require."""


class SolveError(RuntimeError):
    pass


_MIN_PSIA = 1e-3
_MAX_PSIA = 1e7
_MIN_LOG_PSIA = math.log(_MIN_PSIA)
_MAX_LOG_PSIA = math.log(_MAX_PSIA)


class _Problem:
    """Bookkeeping for one solve: which pressures are unknown, which nodes
    contribute equations, and how to evaluate them."""

    def __init__(
        self,
        network: Network,
        settings: ProjectSettings,
        mode: SolveMode,
        solve_for: str | None = None,
        time: float | None = None,
        extra_fixed: dict[str, float] | None = None,
        node_temperatures_R: dict[str, float] | None = None,
    ):
        self.net = network
        self.settings = settings
        self.mode = mode
        self.gas = settings.gas
        self.t_flow = settings.t_flow_R
        self.t_ref = settings.t_ref_R
        self.warnings: list[str] = []

        # Transient support: ``time`` drives scheduled valve actuation, and
        # ``extra_fixed`` pins the dynamic vessel pressures for the quasi-steady
        # inner solve. A pinned vessel contributes no mass-balance equation --
        # its imbalance is exactly the dm/dt the integrator is tracking.
        self.time = time
        self.extra_fixed = dict(extra_fixed or {})
        self.node_temperatures_R = dict(node_temperatures_R or {})

        self.sink = network.sink
        self.boundary_node_id: str | None = None
        self.saturated_regulators: set[str] = set()
        self.excluded_nodes: set[str] = set()
        self.excluded_components: set[str] = set()

        if mode is SolveMode.BACKWARD:
            self.boundary_node_id = self._pick_backward_boundary(solve_for)

        self.fixed: dict[str, float] = {}
        self.unknowns: list[str] = []
        self.equations: list[str] = []
        self._classify()

    # --- setup -------------------------------------------------------------

    def _pick_backward_boundary(self, solve_for: str | None) -> str:
        """Which node's pressure the backward solve reports.

        Either a named regulator's outlet (everything upstream of it is then
        irrelevant and dropped from the solve) or, by default, the single
        source node.
        """
        if solve_for is not None:
            comp = self.net.components.get(solve_for)
            if comp is None:
                raise SolveError(f"solve_for references unknown component {solve_for!r}")
            if not comp.is_regulator:
                raise SolveError(
                    f"solve_for must name a regulator; {comp.name!r} is a "
                    f"{comp.type.value}"
                )
            # The regulator sets its outlet pressure, so nothing upstream of it
            # influences the answer. Drop that side of the network.
            self._exclude_upstream_of(comp)
            return comp.to_node

        sources = self.net.source_nodes
        if len(sources) != 1:
            raise SolveError(
                "Backward mode solves for one supply pressure, but the network "
                f"has {len(sources)} sources. Name a regulator with solve_for="
                "<component id> to size that instead."
            )
        return sources[0].id

    def _exclude_upstream_of(self, regulator: FlowComponent) -> None:
        """Mark the sub-network on the inlet side of ``regulator`` as ignored."""
        self.excluded_components.add(regulator.id)
        seen = {regulator.from_node}
        queue = [regulator.from_node]
        while queue:
            current = queue.pop()
            for comp in self.net.components_at(current):
                if comp.id == regulator.id:
                    continue
                other = comp.to_node if comp.from_node == current else comp.from_node
                self.excluded_components.add(comp.id)
                if other not in seen and other != regulator.to_node:
                    seen.add(other)
                    queue.append(other)
        self.excluded_nodes = seen

    def _active_components(self) -> list[FlowComponent]:
        return [
            c
            for c in self.net.components.values()
            if c.id not in self.excluded_components
        ]

    def _regulated_nodes(self) -> dict[str, float]:
        """Outlet nodes held at a setpoint by an active regulator."""
        held: dict[str, float] = {}
        for comp in self._active_components():
            if not comp.is_regulator or comp.id in self.saturated_regulators:
                continue
            if not comp.is_open or comp.outlet_setpoint_psia is None:
                continue
            if comp.to_node == self.boundary_node_id:
                continue  # that pressure is the thing we are solving for
            held[comp.to_node] = comp.outlet_setpoint_psia
        return held

    def _classify(self) -> None:
        """Split nodes into fixed-pressure, unknown-pressure and equation sets."""
        self.fixed = {}
        self.unknowns = []
        self.equations = []
        regulated = self._regulated_nodes()

        for node_id, node in self.net.nodes.items():
            if node_id in self.excluded_nodes:
                continue

            if node_id in self.extra_fixed:
                self.fixed[node_id] = self.extra_fixed[node_id]
                continue

            if node_id == self.boundary_node_id:
                self.unknowns.append(node_id)  # this is the answer
                continue

            if node_id in regulated:
                self.fixed[node_id] = regulated[node_id]
                continue

            fixed_p = node.fixed_pressure_psia
            if fixed_p is not None:
                self.fixed[node_id] = fixed_p
            else:
                self.unknowns.append(node_id)

            # Which nodes impose a mass-balance equation
            if node.kind is NodeKind.SOURCE:
                continue  # a supply absorbs whatever imbalance exists
            if node.kind is NodeKind.SINK:
                draw = self._sink_draw()
                if fixed_p is None:
                    # Pressure is unknown: either the engine's draw pins it, or
                    # it is a plain junction-style balance.
                    self.equations.append(node_id)
                elif draw is not None and self.mode is SolveMode.BACKWARD:
                    # Both pinned. The extra equation is what the supply
                    # pressure (the backward unknown) absorbs.
                    self.equations.append(node_id)
                # Forward mode with both pinned: the pressure wins and the
                # target flow is only reported against, so no equation here.
                continue
            if fixed_p is None:
                self.equations.append(node_id)

        if len(self.equations) != len(self.unknowns):
            raise SolveError(self._describe_ill_posed())

    def _describe_ill_posed(self) -> str:
        """Say what is actually wrong, in terms of the boundary conditions."""
        n_unknown = len(self.unknowns)
        n_equations = len(self.equations)

        if self.mode is SolveMode.BACKWARD and self._sink_draw() is None:
            return (
                f"Backward mode sizes the supply from what the engine demands, "
                f"but {self.sink.name!r} only specifies a pressure. Set its "
                f"target mass flow as well (Specify: 'Both'), or run forward "
                f"mode instead."
            )

        return (
            f"Ill-posed problem: {n_unknown} unknown pressure(s) against "
            f"{n_equations} equation(s). Every junction needs one mass balance, "
            f"the supply fixes a pressure, and the engine supplies the extra "
            f"condition. Check the source and engine boundary conditions."
        )

    def _sink_draw(self) -> float | None:
        """Mass flow the engine pulls, when that is a boundary condition."""
        node = self.sink
        if node.sink_spec in (SinkSpec.MASS_FLOW, SinkSpec.BOTH):
            return node.target_mdot_kgs
        return None

    # --- evaluation --------------------------------------------------------

    def pressures(self, x: np.ndarray) -> dict[str, float]:
        p = dict(self.fixed)
        # Clamp before exponentiating: a Newton step on a badly conditioned
        # (or genuinely degenerate) system can otherwise overflow the float.
        clamped = np.clip(x, _MIN_LOG_PSIA, _MAX_LOG_PSIA)
        for node_id, log_p in zip(self.unknowns, clamped):
            p[node_id] = math.exp(log_p)
        return p

    def node_temperature_R(self, node_id: str) -> float:
        """Flowing temperature at a node, degR.

        Only vessels deviate from the project temperature, and only during a
        transient run, where their isentropic cooling genuinely drives the
        answer.
        """
        return self.node_temperatures_R.get(node_id, self.t_flow)

    def component_mdot(self, comp: FlowComponent, p: dict[str, float]) -> float:
        """Signed mass flow along ``comp`` in its from -> to direction.

        The Cv equation is evaluated at the *upstream* node's temperature,
        which is what makes a cooling accumulator's discharge come out right.
        """
        cv = comp.effective_cv(self.time)
        if cv <= 0.0:
            return 0.0
        pa = p.get(comp.from_node)
        pb = p.get(comp.to_node)
        if pa is None or pb is None:
            return 0.0

        if comp.is_regulator and comp.outlet_setpoint_psia is not None:
            if comp.id not in self.saturated_regulators:
                # An unsaturated regulator holds its outlet node; the flow it
                # passes is set by the rest of the network, not by this edge.
                # Returning the Cv-limited value here would double-count, so
                # the outlet node is a fixed boundary and carries no equation.
                return mass_flow(
                    cv,
                    pa,
                    comp.outlet_setpoint_psia,
                    self.gas,
                    self.node_temperature_R(comp.from_node),
                    self.t_ref,
                )

        if pa >= pb:
            return mass_flow(
                cv, pa, pb, self.gas, self.node_temperature_R(comp.from_node), self.t_ref
            )
        if not comp.allows_reverse_flow:
            return 0.0
        return -mass_flow(
            cv, pb, pa, self.gas, self.node_temperature_R(comp.to_node), self.t_ref
        )

    def residuals(self, x: np.ndarray) -> np.ndarray:
        p = self.pressures(x)
        out = np.zeros(len(self.equations))
        for i, node_id in enumerate(self.equations):
            net = 0.0
            for comp in self.net.components_at(node_id):
                if comp.id in self.excluded_components:
                    continue
                mdot = self.component_mdot(comp, p)
                net += mdot if comp.to_node == node_id else -mdot
            if node_id == self.sink.id:
                draw = self._sink_draw()
                if draw is not None:
                    net -= draw
            out[i] = net
        return out


# --- initial guess ---------------------------------------------------------


def _demand_per_component(net: Network, sink_mdot: float) -> dict[str, float]:
    """Push the engine's demand upstream, splitting evenly at branch points.

    Only an initial guess -- the solve corrects an uneven split. Exact for a
    pure series chain, which makes those cases converge immediately.
    """
    demand: dict[str, float] = {}
    visited = {net.sink.id}
    frontier: list[tuple[str, float]] = [(net.sink.id, sink_mdot)]

    while frontier:
        node_id, flow = frontier.pop()
        upstream = [
            c
            for c in net.components_at(node_id)
            if (c.from_node if c.to_node == node_id else c.to_node) not in visited
        ]
        if not upstream:
            continue
        share = flow / len(upstream)
        for comp in upstream:
            demand[comp.id] = demand.get(comp.id, 0.0) + share
            other = comp.from_node if comp.to_node == node_id else comp.to_node
            if other not in visited:
                visited.add(other)
                frontier.append((other, share))
    return demand


def _initial_guess(
    problem: _Problem, warm_start: dict[str, float] | None = None
) -> np.ndarray:
    """Walk upstream from the engine applying the single-component inverse.

    ``warm_start`` (the previous step's pressures, during a transient run)
    short-circuits the walk, which matters because the transient integrator
    calls this thousands of times.
    """
    if warm_start is not None and all(n in warm_start for n in problem.unknowns):
        return np.array(
            [math.log(max(warm_start[n], _MIN_PSIA)) for n in problem.unknowns]
        )

    net = problem.net
    settings = problem.settings
    sink_mdot = problem._sink_draw() or 0.0

    sink_p = problem.fixed.get(net.sink.id)
    if sink_p is None:
        sink_p = psig_to_psia(net.sink.target_pressure_psig or 150.0)

    demand = _demand_per_component(net, sink_mdot)
    guess = {net.sink.id: sink_p}

    visited = {net.sink.id}
    frontier = [net.sink.id]
    while frontier:
        node_id = frontier.pop()
        for comp in net.components_at(node_id):
            if comp.id in problem.excluded_components:
                continue
            other = comp.from_node if comp.to_node == node_id else comp.to_node
            if other in visited:
                continue
            visited.add(other)
            mdot = demand.get(comp.id, 0.0)
            cv = comp.effective_cv(problem.time)
            try:
                if mdot > 0.0 and cv > 0.0:
                    upstream_p = p1_for_mass_flow(
                        mdot,
                        cv,
                        guess[node_id],
                        settings.gas,
                        settings.t_flow_R,
                        settings.t_ref_R,
                    )
                else:
                    upstream_p = guess[node_id]
            except ValueError:
                upstream_p = guess[node_id] * 2.0
            guess[other] = upstream_p
            frontier.append(other)

    fallback = max(guess.values(), default=sink_p)
    return np.array(
        [math.log(max(guess.get(n, fallback), _MIN_PSIA)) for n in problem.unknowns]
    )


# --- driver ----------------------------------------------------------------


def _solve_once(
    problem: _Problem, warm_start: dict[str, float] | None = None
) -> tuple[dict[str, float], bool, float, int]:
    if not problem.unknowns:
        return dict(problem.fixed), True, 0.0, 0

    x0 = _initial_guess(problem, warm_start)

    sol = root(problem.residuals, x0, method="hybr", tol=1e-12)
    x = sol.x
    converged = bool(sol.success)
    resid = float(np.linalg.norm(problem.residuals(x)))
    nfev = int(getattr(sol, "nfev", 0))

    if not converged or not np.isfinite(resid) or resid > 1e-9:
        # hybr can stall on the derivative kink at the choke point; a bounded
        # least-squares pass is slower but far more forgiving.
        ls = least_squares(
            problem.residuals,
            x0,
            method="trf",
            xtol=1e-14,
            ftol=1e-14,
            gtol=1e-14,
            max_nfev=5000,
        )
        ls_resid = float(np.linalg.norm(problem.residuals(ls.x)))
        if ls_resid < resid or not converged:
            x, resid, nfev = ls.x, ls_resid, int(ls.nfev)
            converged = ls_resid < 1e-7

    return problem.pressures(x), converged, resid, nfev


def _check_regulators(problem: _Problem, pressures: dict[str, float]) -> bool:
    """Flag regulators that cannot hold their setpoint. Returns True if any
    newly saturated, meaning the solve must be repeated."""
    changed = False
    for comp in problem._active_components():
        if not comp.is_regulator or comp.id in problem.saturated_regulators:
            continue
        setpoint = comp.outlet_setpoint_psia
        if setpoint is None or not comp.is_open:
            continue
        p_in = pressures.get(comp.from_node)
        if p_in is None:
            continue

        if p_in <= setpoint:
            problem.saturated_regulators.add(comp.id)
            problem.warnings.append(
                f"{comp.name!r}: inlet {psia_to_psig(p_in):.1f} psig is at or below "
                f"its {comp.outlet_setpoint_psig:.1f} psig setpoint; it cannot "
                "regulate and is modelled as a plain Cv restriction"
            )
            changed = True
            continue

        demanded = 0.0
        for other in problem.net.components_at(comp.to_node):
            if other.id == comp.id or other.id in problem.excluded_components:
                continue
            mdot = problem.component_mdot(other, pressures)
            demanded += mdot if other.from_node == comp.to_node else -mdot

        capacity = mass_flow(
            comp.effective_cv(),
            p_in,
            setpoint,
            problem.gas,
            problem.t_flow,
            problem.t_ref,
        )
        if demanded > capacity * (1.0 + 1e-9):
            problem.saturated_regulators.add(comp.id)
            problem.warnings.append(
                f"{comp.name!r} is flow-saturated: Cv={comp.effective_cv():g} passes "
                f"{capacity * 1e3:.1f} g/s at its setpoint but {demanded * 1e3:.1f} g/s "
                "is demanded; outlet pressure will droop below setpoint"
            )
            changed = True

        if comp.inlet_min_psig is not None and psia_to_psig(p_in) < comp.inlet_min_psig:
            problem.warnings.append(
                f"{comp.name!r}: inlet {psia_to_psig(p_in):.1f} psig is below its "
                f"rated minimum of {comp.inlet_min_psig:.1f} psig"
            )
        if comp.inlet_max_psig is not None and psia_to_psig(p_in) > comp.inlet_max_psig:
            problem.warnings.append(
                f"{comp.name!r}: inlet {psia_to_psig(p_in):.1f} psig exceeds its "
                f"rated maximum of {comp.inlet_max_psig:.1f} psig"
            )
    return changed


def solve_steady(
    network: Network,
    settings: ProjectSettings,
    mode: SolveMode = SolveMode.FORWARD,
    solve_for: str | None = None,
    validate: bool = True,
) -> SteadyResult:
    """Solve a network for node pressures and component mass flows.

    ``solve_for`` names a regulator whose outlet pressure the backward mode
    should size, instead of the supply node's pressure.
    """
    if validate:
        network.raise_on_errors()

    problem = _Problem(network, settings, mode, solve_for)

    if mode is SolveMode.FORWARD and network.sink.sink_spec is SinkSpec.BOTH:
        problem.warnings.append(
            "Engine has both a target pressure and a target mass flow. Forward "
            "mode uses the pressure; compare the delivered flow against the "
            "target, or run backward mode to size the supply."
        )

    pressures: dict[str, float] = {}
    converged = False
    resid = 0.0
    iterations = 0
    for _ in range(len(network.components) + 2):  # bounded regulator re-solves
        problem._classify()
        pressures, converged, resid, nfev = _solve_once(problem)
        iterations += nfev
        if not _check_regulators(problem, pressures):
            break

    flows: dict[str, ComponentFlow] = {}
    for comp in network.components.values():
        if comp.id in problem.excluded_components:
            continue
        p_from = pressures.get(comp.from_node)
        p_to = pressures.get(comp.to_node)
        if p_from is None or p_to is None:
            continue
        mdot = problem.component_mdot(comp, pressures)
        hi, lo = (p_from, p_to) if mdot >= 0 else (p_to, p_from)
        _, regime = mass_flow_with_regime(
            comp.effective_cv(), hi, lo, problem.gas, problem.t_flow, problem.t_ref
        )
        flows[comp.id] = ComponentFlow(
            component_id=comp.id,
            name=comp.name,
            mdot_kgs=mdot,
            regime=regime,
            p_in_psia=p_from,
            p_out_psia=p_to,
            cv=comp.effective_cv(),
        )

    warnings = list(problem.warnings)
    if not converged:
        warnings.append(_diagnose_nonconvergence(problem, pressures, flows, resid))
    warnings.extend(_rating_warnings(network, pressures))

    return SteadyResult(
        node_pressures_psia=pressures,
        component_flows=flows,
        mode=mode.value,
        converged=converged,
        iterations=iterations,
        residual_norm=resid,
        temperature_R=settings.t_flow_R,
        warnings=warnings,
        solved_boundary_psia=(
            pressures.get(problem.boundary_node_id)
            if problem.boundary_node_id
            else None
        ),
    )


def solve_quasi_steady(
    network: Network,
    settings: ProjectSettings,
    time: float,
    fixed_pressures: dict[str, float],
    warm_start: dict[str, float] | None = None,
    node_temperatures_R: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """One inner solve for the transient integrator.

    Vessel pressures are pinned at their current state values; everything else
    in the network is solved instantaneously, which is the quasi-steady
    assumption: line volumes are negligible next to vessel volumes, so the
    pipework reaches equilibrium far faster than the tanks drain.

    Returns ``(node pressures psia, component mass flows kg/s)``, the latter
    signed in each component's from -> to direction.
    """
    problem = _Problem(
        network,
        settings,
        SolveMode.FORWARD,
        time=time,
        extra_fixed=fixed_pressures,
        node_temperatures_R=node_temperatures_R,
    )
    pressures, _converged, _resid, _nfev = _solve_once(problem, warm_start)
    flows = {
        comp.id: problem.component_mdot(comp, pressures)
        for comp in network.components.values()
    }
    return pressures, flows


def _diagnose_nonconvergence(
    problem: _Problem,
    pressures: dict[str, float],
    flows: dict[str, ComponentFlow],
    resid: float,
) -> str:
    """Explain a failed solve in terms of the physics, not the numerics.

    The common cause is a genuinely over-determined problem rather than a bad
    initial guess: when every component feeding the engine is choked, the flow
    it passes depends only on its *upstream* pressure, so the engine's
    back-pressure has no influence on it. Forward mode with the supply pressure
    pinned AND a target mass flow then imposes two independent constraints on
    one degree of freedom, and no engine pressure can satisfy both.
    """
    sink = problem.sink
    draw = problem._sink_draw()

    if problem.mode is SolveMode.FORWARD and draw is not None:
        feeds = [
            flows[c.id]
            for c in problem.net.components_at(sink.id)
            if c.id in flows
        ]
        if feeds and all(f.regime is Regime.CHOKED for f in feeds):
            delivered = sum(f.mdot_kgs for f in feeds)
            return (
                f"Over-determined: every feed into {sink.name!r} is choked, so its "
                f"flow is set by the supply pressure alone and the engine "
                f"back-pressure cannot change it. At the given supply the feed "
                f"delivers {delivered * 1e3:.1f} g/s, not the requested "
                f"{draw * 1e3:.1f} g/s. Run backward mode to find the supply "
                f"pressure that gives {draw * 1e3:.1f} g/s, or set the engine's "
                f"pressure instead of its mass flow."
            )

    return (
        f"Solver did not fully converge (residual {resid:.3e} kg/s). "
        "Results are indicative only -- check the boundary conditions."
    )


def _rating_warnings(network: Network, pressures: dict[str, float]) -> list[str]:
    out: list[str] = []
    for comp in network.components.values():
        if comp.max_pressure_psig is None:
            continue
        p_in = pressures.get(comp.from_node)
        if p_in is None:
            continue
        if comp.exceeds_rating(psia_to_psig(p_in)):
            out.append(
                f"{comp.name!r} sees {psia_to_psig(p_in):.1f} psig, above its "
                f"{comp.max_pressure_psig:.1f} psig rating"
            )
    return out
