"""Component and node data classes.

Topology model
--------------
The network splits into two kinds of object, which is what lets one solver
handle series chains, splits, merges and parallel paths uniformly:

* :class:`Node` -- a place where a pressure exists. Plain junctions, tees,
  accumulators, the supply and the engine are all nodes. A node may carry a
  *volume* (making it dynamic) or a *boundary condition* (fixing its pressure
  or its draw).
* :class:`FlowComponent` -- a two-port restriction between exactly two nodes,
  characterised by a Cv. Pipes, valves, hoses, adapters, reducers and
  regulators are all of this kind.

A tee is therefore a node, not an edge: its three legs sit at one pressure.
Tee and elbow losses are conventionally folded into the Cv of the adjacent
line, which is exactly what the validated reference chains do. If you want to
charge a tee its own loss, give the branch leg a dedicated component.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from ..physics.flow import combine_series_cv
from ..units import T_FLOW_R_DEFAULT, psig_to_psia


class ComponentType(str, Enum):
    """Two-port flow restrictions."""

    PIPE = "pipe"
    ELBOW = "elbow"
    REDUCER = "reducer"
    ADAPTER = "adapter"
    HOSE = "hose"
    BALL_VALVE = "ball_valve"
    CHECK_VALVE = "check_valve"
    SOLENOID_VALVE = "solenoid_valve"
    REGULATOR = "regulator"
    ORIFICE = "orifice"

    @property
    def is_valve(self) -> bool:
        return self in {
            ComponentType.BALL_VALVE,
            ComponentType.CHECK_VALVE,
            ComponentType.SOLENOID_VALVE,
        }


class NodeKind(str, Enum):
    """Places a pressure exists."""

    JUNCTION = "junction"
    """A plain connection point, including tees. No volume, no boundary."""

    TEE = "tee"
    """Drawn as a tee on the canvas; physically identical to a junction."""

    SOURCE = "source"
    """Supply: an ideal constant pressure, or a finite-volume tank."""

    ACCUMULATOR = "accumulator"
    """A vessel with volume hanging on the network. Dynamic in transient runs."""

    SINK = "sink"
    """The engine/chamber black box. Always a boundary condition."""

    @property
    def is_vessel(self) -> bool:
        return self in {NodeKind.ACCUMULATOR, NodeKind.SOURCE}


class SourceMode(str, Enum):
    IDEAL_CONSTANT = "ideal_constant"
    """Infinite supply held at a fixed pressure."""

    FINITE_VOLUME = "finite_volume"
    """A real tank that blows down during a transient run."""


class SinkSpec(str, Enum):
    """Which engine quantity is pinned as the boundary condition."""

    MASS_FLOW = "mass_flow"
    """Target mdot fixed; the sink pressure is solved for."""

    PRESSURE = "pressure"
    """Target pressure fixed; the mdot that results is solved for."""

    BOTH = "both"
    """Both fixed -- over-specified downstream, so the SOURCE pressure becomes
    the unknown. This is the backward/sizing mode that produces the reference
    cases."""


class VesselModel(str, Enum):
    """Which transient formulation to use for a vessel. Mirrors solver.vessel."""

    AUTO = "auto"
    ISENTROPIC = "isentropic"
    ENERGY = "energy"


_id_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}{next(_id_counter)}"


@dataclass
class ValveSchedule:
    """A scripted valve actuation for transient runs.

    ``open_time``/``close_time`` are in seconds; ``ramp_time`` is how long the
    Cv takes to travel between shut and full open. Outside the scheduled
    window the valve sits at its initial state.
    """

    open_time: float | None = None
    close_time: float | None = None
    ramp_time: float = 0.0
    initially_open: bool = True

    def fraction_open(self, t: float) -> float:
        """Fractional Cv at time ``t``, in [0, 1]."""
        frac = 1.0 if self.initially_open else 0.0

        if self.open_time is not None and t >= self.open_time:
            frac = self._ramp(t - self.open_time, rising=True)
        if self.close_time is not None and t >= self.close_time:
            # A later close overrides an earlier open.
            if self.open_time is None or self.close_time >= self.open_time:
                frac = self._ramp(t - self.close_time, rising=False)
        return min(1.0, max(0.0, frac))

    def _ramp(self, dt: float, rising: bool) -> float:
        if self.ramp_time <= 0.0:
            return 1.0 if rising else 0.0
        progress = min(1.0, dt / self.ramp_time)
        return progress if rising else 1.0 - progress


@dataclass
class FlowComponent:
    """A two-port restriction between two nodes."""

    type: ComponentType
    from_node: str
    to_node: str
    id: str = field(default_factory=lambda: _new_id("c"))
    name: str = ""

    # Real-world identification
    manufacturer: str = ""
    part_number: str = ""
    library_key: str = ""

    # Flow characteristic
    cv: float = 1.0

    # Connections and rating
    inlet_size: str = ""
    outlet_size: str = ""
    connection_type: str = ""
    max_pressure_psig: float | None = None

    # Valve state
    is_open: bool = True
    schedule: ValveSchedule | None = None

    # Regulator-specific
    outlet_setpoint_psig: float | None = None
    inlet_min_psig: float | None = None
    inlet_max_psig: float | None = None
    outlet_min_psig: float | None = None
    outlet_max_psig: float | None = None

    # Canvas placement (ignored by the solver)
    x: float = 0.0
    y: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.type.value.replace("_", " ").title()

    # --- behaviour ---------------------------------------------------------

    @property
    def allows_reverse_flow(self) -> bool:
        return self.type is not ComponentType.CHECK_VALVE

    @property
    def is_regulator(self) -> bool:
        return self.type is ComponentType.REGULATOR

    @property
    def outlet_setpoint_psia(self) -> float | None:
        if self.outlet_setpoint_psig is None:
            return None
        return psig_to_psia(self.outlet_setpoint_psig)

    def effective_cv(self, t: float | None = None) -> float:
        """Cv accounting for shut state and any scheduled actuation.

        ``t`` is the transient time; pass ``None`` for steady-state, which uses
        the static ``is_open`` flag.
        """
        if self.cv <= 0.0:
            return 0.0
        if t is not None and self.schedule is not None:
            return self.cv * self.schedule.fraction_open(t)
        return self.cv if self.is_open else 0.0

    def exceeds_rating(self, pressure_psig: float) -> bool:
        return (
            self.max_pressure_psig is not None
            and pressure_psig > self.max_pressure_psig
        )


@dataclass
class Node:
    """A point in the network where a pressure exists."""

    kind: NodeKind = NodeKind.JUNCTION
    id: str = field(default_factory=lambda: _new_id("n"))
    name: str = ""

    # --- source ------------------------------------------------------------
    source_mode: SourceMode = SourceMode.IDEAL_CONSTANT
    supply_pressure_psig: float | None = None
    supply_temperature_R: float = T_FLOW_R_DEFAULT

    # --- vessel (accumulator, or a finite-volume source) -------------------
    volume_m3: float | None = None
    charge_pressure_psig: float | None = None
    charge_temperature_R: float = T_FLOW_R_DEFAULT
    vessel_model: VesselModel = VesselModel.AUTO

    # --- sink (the engine black box) ---------------------------------------
    sink_spec: SinkSpec = SinkSpec.BOTH
    target_mdot_kgs: float | None = None
    target_pressure_psig: float | None = None

    # Canvas placement (ignored by the solver)
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    """Degrees clockwise, for laying the diagram out. Purely presentational:
    it moves where the connection legs sit on the canvas and nothing else. The
    solver never reads it."""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.kind.value.replace("_", " ").title()

    @property
    def is_dynamic(self) -> bool:
        """True if this node carries gas mass that changes during a transient."""
        if self.kind is NodeKind.ACCUMULATOR:
            return self.volume_m3 is not None and self.volume_m3 > 0.0
        if self.kind is NodeKind.SOURCE:
            return (
                self.source_mode is SourceMode.FINITE_VOLUME
                and self.volume_m3 is not None
                and self.volume_m3 > 0.0
            )
        return False

    @property
    def fixed_pressure_psia(self) -> float | None:
        """Steady-state pressure boundary imposed by this node, if any."""
        if self.kind is NodeKind.SOURCE and self.supply_pressure_psig is not None:
            return psig_to_psia(self.supply_pressure_psig)
        if self.kind is NodeKind.ACCUMULATOR and self.charge_pressure_psig is not None:
            # In steady state an accumulator is just a pressurised junction.
            return None
        if self.kind is NodeKind.SINK and self.sink_spec in (
            SinkSpec.PRESSURE,
            SinkSpec.BOTH,
        ):
            if self.target_pressure_psig is not None:
                return psig_to_psia(self.target_pressure_psig)
        return None

    @property
    def initial_pressure_psia(self) -> float | None:
        """Starting pressure for a transient run."""
        if self.kind is NodeKind.ACCUMULATOR and self.charge_pressure_psig is not None:
            return psig_to_psia(self.charge_pressure_psig)
        if self.kind is NodeKind.SOURCE:
            if self.supply_pressure_psig is not None:
                return psig_to_psia(self.supply_pressure_psig)
            if self.charge_pressure_psig is not None:
                return psig_to_psia(self.charge_pressure_psig)
        return None


def series_component(
    components: list[FlowComponent], from_node: str, to_node: str, name: str = ""
) -> FlowComponent:
    """Collapse a chain of components into one equivalent element.

    Convenience for building the reference chains and for the GUI's "lump this
    run" action. Uses the reciprocal-square-sum rule from
    :func:`pidsim.physics.flow.combine_series_cv`.
    """
    if not components:
        raise ValueError("series_component() requires at least one component")
    return FlowComponent(
        type=ComponentType.PIPE,
        from_node=from_node,
        to_node=to_node,
        cv=combine_series_cv(c.cv for c in components),
        name=name or " + ".join(c.name for c in components),
    )
