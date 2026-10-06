"""Steady-state solver: the reference cases rebuilt as real networks, plus
the branching behaviour the nodal formulation is there to provide."""

from __future__ import annotations

import pytest

from pidsim.model import (
    ComponentType,
    FlowComponent,
    Network,
    Node,
    NodeKind,
    ProjectSettings,
    SinkSpec,
)
from pidsim.solver.steady import SolveError, SolveMode, solve_steady
from pidsim.units import psia_to_psig

TOL = 0.01
MANIFOLD_PSIG = 150.0


def o2_settings() -> ProjectSettings:
    return ProjectSettings(gas_name="O2", t_flow_R=530.0, t_ref_R=530.0)


def ch4_settings() -> ProjectSettings:
    return ProjectSettings(gas_name="CH4", t_flow_R=530.0, t_ref_R=530.0)


def _supply(pressure_psig: float | None = None) -> Node:
    return Node(
        kind=NodeKind.SOURCE,
        id="supply",
        name="Regulator Outlet",
        supply_pressure_psig=pressure_psig if pressure_psig is not None else 0.0,
    )


def _engine(mdot: float, spec: SinkSpec = SinkSpec.BOTH) -> Node:
    return Node(
        kind=NodeKind.SINK,
        id="engine",
        name="Engine",
        sink_spec=spec,
        target_mdot_kgs=mdot,
        target_pressure_psig=MANIFOLD_PSIG,
    )


def _line(cv: float, a: str, b: str, cid: str, name: str = "") -> FlowComponent:
    return FlowComponent(
        type=ComponentType.PIPE,
        from_node=a,
        to_node=b,
        id=cid,
        cv=cv,
        name=name or cid,
    )


def single_branch_network(mdot: float = 0.110, cv: float = 0.732) -> Network:
    net = Network(name="O2 single branch")
    net.add_node(_supply())
    net.add_node(_engine(mdot))
    net.add_component(_line(cv, "supply", "engine", "branch"))
    return net


def two_branch_network() -> Network:
    """Reference case 2: one main chain feeding two identical branches."""
    net = Network(name="O2 main + 2 branches")
    net.add_node(_supply())
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Branch Tee"))
    net.add_node(_engine(0.220))
    net.add_component(_line(2.394, "supply", "tee", "main", "Main Chain"))
    net.add_component(_line(0.732, "tee", "engine", "branch_a", "Branch A"))
    net.add_component(_line(0.732, "tee", "engine", "branch_b", "Branch B"))
    return net


# --- reference cases through the full model/solver stack -------------------


def test_case_1_through_solver():
    result = solve_steady(
        single_branch_network(), o2_settings(), SolveMode.BACKWARD
    )
    assert result.converged
    assert result.solved_boundary_psia == pytest.approx(418.2, rel=TOL)
    assert psia_to_psig(result.solved_boundary_psia) == pytest.approx(403.5, rel=TOL)
    assert result.component_flows["branch"].regime.value == "choked"
    assert result.component_flows["branch"].mdot_gs == pytest.approx(110.0, rel=1e-6)


def test_case_2_through_solver():
    result = solve_steady(two_branch_network(), o2_settings(), SolveMode.BACKWARD)
    assert result.converged
    assert result.solved_boundary_psia == pytest.approx(473.2, rel=TOL)
    # The tee sits at the case-1 branch inlet pressure.
    assert result.node_pressures_psia["tee"] == pytest.approx(418.2, rel=TOL)


def test_case_3_through_solver():
    net = single_branch_network(mdot=0.060, cv=1.883)
    result = solve_steady(net, ch4_settings(), SolveMode.BACKWARD)
    assert result.converged
    assert result.solved_boundary_psia == pytest.approx(197.2, rel=TOL)
    assert result.component_flows["branch"].regime.value == "subsonic"


# --- forward / backward consistency ---------------------------------------


def test_forward_inverts_backward():
    back = solve_steady(two_branch_network(), o2_settings(), SolveMode.BACKWARD)
    supply_psig = psia_to_psig(back.solved_boundary_psia)

    net = two_branch_network()
    net.nodes["supply"].supply_pressure_psig = supply_psig
    net.nodes["engine"].sink_spec = SinkSpec.PRESSURE

    fwd = solve_steady(net, o2_settings(), SolveMode.FORWARD)
    assert fwd.converged
    delivered = sum(
        fwd.component_flows[c].mdot_kgs for c in ("branch_a", "branch_b")
    )
    assert delivered == pytest.approx(0.220, rel=1e-4)
    assert fwd.node_pressures_psia["tee"] == pytest.approx(
        back.node_pressures_psia["tee"], rel=1e-4
    )


def test_forward_with_mass_flow_spec_solves_engine_pressure():
    """Subsonic feed: the engine's back-pressure is determined by its draw."""
    net = single_branch_network(mdot=0.060, cv=1.883)
    net.nodes["supply"].supply_pressure_psig = 182.54
    net.nodes["engine"].sink_spec = SinkSpec.MASS_FLOW

    result = solve_steady(net, ch4_settings(), SolveMode.FORWARD)
    assert result.converged
    assert psia_to_psig(result.node_pressures_psia["engine"]) == pytest.approx(
        150.0, rel=0.01
    )


def test_choked_feed_with_mass_flow_spec_is_reported_as_over_determined():
    """A choked feed's flow is set upstream, so demanding a different mdot in
    forward mode has no solution. The solver must say why, not just fail."""
    net = two_branch_network()
    net.nodes["supply"].supply_pressure_psig = 458.5
    net.nodes["engine"].sink_spec = SinkSpec.MASS_FLOW
    net.nodes["engine"].target_mdot_kgs = 0.050  # far below what 473 psia delivers

    result = solve_steady(net, o2_settings(), SolveMode.FORWARD)
    assert not result.converged
    assert any("Over-determined" in w and "choked" in w for w in result.warnings)
    assert any("backward mode" in w for w in result.warnings)


# --- branching behaviour ---------------------------------------------------


def test_symmetric_branches_split_evenly():
    result = solve_steady(two_branch_network(), o2_settings(), SolveMode.BACKWARD)
    a = result.component_flows["branch_a"].mdot_kgs
    b = result.component_flows["branch_b"].mdot_kgs
    assert a == pytest.approx(b, rel=1e-6)
    assert a + b == pytest.approx(0.220, rel=1e-6)


def test_asymmetric_branches_do_not_split_evenly():
    """The whole point of solving the junction pressure implicitly."""
    net = two_branch_network()
    net.components["branch_b"].cv = 0.366  # half the Cv of branch A

    result = solve_steady(net, o2_settings(), SolveMode.BACKWARD)
    a = result.component_flows["branch_a"].mdot_kgs
    b = result.component_flows["branch_b"].mdot_kgs

    assert a + b == pytest.approx(0.220, rel=1e-6)
    assert a > b
    # Both legs are choked here, where flow is exactly proportional to Cv.
    assert a / b == pytest.approx(2.0, rel=1e-3)


def test_merging_branches_are_solvable():
    """Split then merge back: a parallel path, not a rejected 'loop'."""
    net = Network(name="split and merge")
    net.add_node(_supply(500.0))
    net.add_node(Node(kind=NodeKind.TEE, id="split", name="Split"))
    net.add_node(Node(kind=NodeKind.TEE, id="merge", name="Merge"))
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=MANIFOLD_PSIG,
        )
    )
    net.add_component(_line(5.0, "supply", "split", "inlet"))
    net.add_component(_line(1.0, "split", "merge", "leg_a"))
    net.add_component(_line(2.0, "split", "merge", "leg_b"))
    net.add_component(_line(5.0, "merge", "engine", "outlet"))

    issues = net.validate()
    assert not [i for i in issues if i.severity.value == "error"]
    assert any("parallel flow path" in i.message for i in issues)

    result = solve_steady(net, o2_settings(), SolveMode.FORWARD)
    assert result.converged
    a = result.component_flows["leg_a"].mdot_kgs
    b = result.component_flows["leg_b"].mdot_kgs
    inlet = result.component_flows["inlet"].mdot_kgs
    assert a + b == pytest.approx(inlet, rel=1e-6)
    assert b > a  # the higher-Cv leg carries more


def test_three_branches_are_not_hardcoded_to_two():
    net = Network(name="three branches")
    net.add_node(_supply())
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Tee"))
    net.add_node(_engine(0.330))
    net.add_component(_line(2.394, "supply", "tee", "main"))
    for i, cid in enumerate(("b1", "b2", "b3")):
        net.add_component(_line(0.732, "tee", "engine", cid))

    result = solve_steady(net, o2_settings(), SolveMode.BACKWARD)
    assert result.converged
    flows = [result.component_flows[c].mdot_kgs for c in ("b1", "b2", "b3")]
    assert sum(flows) == pytest.approx(0.330, rel=1e-6)
    for f in flows:
        assert f == pytest.approx(0.110, rel=1e-4)
    # Each branch still sees the case-1 inlet pressure.
    assert result.node_pressures_psia["tee"] == pytest.approx(418.2, rel=TOL)


# --- closed valves and check valves ---------------------------------------


def test_closed_valve_blocks_a_branch():
    net = two_branch_network()
    net.components["branch_b"].type = ComponentType.BALL_VALVE
    net.components["branch_b"].is_open = False
    net.nodes["engine"].target_mdot_kgs = 0.110

    result = solve_steady(net, o2_settings(), SolveMode.BACKWARD)
    assert result.component_flows["branch_b"].mdot_kgs == 0.0
    assert result.component_flows["branch_a"].mdot_gs == pytest.approx(110.0, rel=1e-6)


# --- regulators ------------------------------------------------------------


def regulated_network(setpoint_psig: float, reg_cv: float = 1.0) -> Network:
    net = Network(name="regulated")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="bottle",
            name="Bottle",
            supply_pressure_psig=2000.0,
        )
    )
    net.add_node(Node(kind=NodeKind.JUNCTION, id="reg_out", name="Regulator Outlet"))
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=MANIFOLD_PSIG,
        )
    )
    reg = FlowComponent(
        type=ComponentType.REGULATOR,
        from_node="bottle",
        to_node="reg_out",
        id="reg",
        name="Matheson 3200",
        cv=reg_cv,
        outlet_setpoint_psig=setpoint_psig,
    )
    net.add_component(reg)
    net.add_component(_line(1.883, "reg_out", "engine", "chain"))
    return net


def test_regulator_holds_its_setpoint():
    net = regulated_network(setpoint_psig=400.0, reg_cv=5.0)
    result = solve_steady(net, ch4_settings(), SolveMode.FORWARD)
    assert result.converged
    assert psia_to_psig(result.node_pressures_psia["reg_out"]) == pytest.approx(
        400.0, rel=1e-6
    )
    assert not any("saturated" in w for w in result.warnings)


def test_undersized_regulator_is_reported_as_saturated():
    """Cv=0.05 cannot pass what the downstream chain demands at setpoint."""
    net = regulated_network(setpoint_psig=400.0, reg_cv=0.05)
    result = solve_steady(net, ch4_settings(), SolveMode.FORWARD)
    assert any("saturated" in w for w in result.warnings)
    # Having drooped, the outlet now sits below the setpoint.
    assert psia_to_psig(result.node_pressures_psia["reg_out"]) < 400.0


def test_backward_can_size_a_named_regulator():
    """Solve for the setpoint a regulator needs, ignoring its supply side."""
    net = regulated_network(setpoint_psig=400.0)
    net.components["reg"].outlet_setpoint_psig = None
    net.nodes["engine"].sink_spec = SinkSpec.BOTH
    net.nodes["engine"].target_mdot_kgs = 0.060

    result = solve_steady(
        net, ch4_settings(), SolveMode.BACKWARD, solve_for="reg"
    )
    assert result.converged
    # Reference case 3: 1.883 Cv, 60 g/s CH4 into 150 psig needs 197.2 psia.
    assert result.solved_boundary_psia == pytest.approx(197.2, rel=TOL)


def test_solve_for_rejects_a_non_regulator():
    net = two_branch_network()
    with pytest.raises(SolveError, match="must name a regulator"):
        solve_steady(net, o2_settings(), SolveMode.BACKWARD, solve_for="main")


def test_backward_without_a_flow_target_explains_itself():
    """Sizing the supply needs something to size it against."""
    net = single_branch_network()
    net.nodes["engine"].sink_spec = SinkSpec.PRESSURE

    with pytest.raises(SolveError, match="only specifies a pressure"):
        solve_steady(net, o2_settings(), SolveMode.BACKWARD)


def test_backward_needs_a_single_supply():
    net = single_branch_network()
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply2",
            name="Second Supply",
            supply_pressure_psig=400.0,
        )
    )
    net.add_component(_line(1.0, "supply2", "engine", "second"))
    with pytest.raises(SolveError, match="2 sources"):
        solve_steady(net, o2_settings(), SolveMode.BACKWARD)


# --- ratings ---------------------------------------------------------------


def test_overpressure_is_warned_about():
    net = single_branch_network()
    net.components["branch"].max_pressure_psig = 200.0
    result = solve_steady(net, o2_settings(), SolveMode.BACKWARD)
    assert any("rating" in w for w in result.warnings)
