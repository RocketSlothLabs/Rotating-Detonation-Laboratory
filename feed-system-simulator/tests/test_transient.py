"""Transient solver: reference cases 4 and 5 rebuilt as real networks, plus
the isolated/coupled distinction and scheduled valve behaviour."""

from __future__ import annotations

import numpy as np
import pytest

from pidsim.model import (
    ComponentType,
    FlowComponent,
    Network,
    Node,
    NodeKind,
    ProjectSettings,
    SinkSpec,
    ValveSchedule,
    VesselModel,
)
from pidsim.solver.transient import TransientError, solve_transient
from pidsim.units import LITER_TO_M3, psia_to_psig

TOL = 0.01
MANIFOLD_PSIG = 150.0


def o2_settings() -> ProjectSettings:
    return ProjectSettings(gas_name="O2", t_flow_R=530.0, t_ref_R=530.0)


def _engine() -> Node:
    return Node(
        kind=NodeKind.SINK,
        id="engine",
        name="Engine",
        sink_spec=SinkSpec.PRESSURE,
        target_pressure_psig=MANIFOLD_PSIG,
    )


def _accumulator(model: VesselModel = VesselModel.AUTO) -> Node:
    return Node(
        kind=NodeKind.ACCUMULATOR,
        id="acc",
        name="Accumulator",
        volume_m3=10.0 * LITER_TO_M3,
        charge_pressure_psig=500.0,
        charge_temperature_R=530.0,
        vessel_model=model,
    )


def _line(cv: float, a: str, b: str, cid: str, **kw) -> FlowComponent:
    return FlowComponent(
        type=kw.pop("type", ComponentType.PIPE),
        from_node=a,
        to_node=b,
        id=cid,
        cv=cv,
        name=kw.pop("name", cid),
        **kw,
    )


def isolated_network(model: VesselModel = VesselModel.AUTO) -> Network:
    """Reference case 4: accumulator alone, discharging into the manifold."""
    net = Network(name="isolated accumulator")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            name="Supply",
            supply_pressure_psig=500.0,
        )
    )
    net.add_node(_accumulator(model))
    net.add_node(_engine())
    # Shut, with no schedule: the accumulator is genuinely cut off.
    net.add_component(
        _line(
            0.923,
            "supply",
            "acc",
            "feed",
            type=ComponentType.BALL_VALVE,
            name="Isolation Valve",
            is_open=False,
        )
    )
    net.add_component(_line(0.732, "acc", "engine", "discharge", name="Discharge"))
    return net


def coupled_network(model: VesselModel = VesselModel.AUTO) -> Network:
    """Reference case 5: same vessel, still fed through Cv=0.923."""
    net = isolated_network(model)
    net.components["feed"].is_open = True
    return net


# --- reference cases through the full stack -------------------------------


def test_case_4_isolated_through_solver():
    result = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0))
    snap = result.at(1.0)

    assert psia_to_psig(snap["node_pressure_psia"]["acc"]) == pytest.approx(
        331.4, rel=TOL
    )
    assert snap["component_mdot_kgs"]["discharge"] * 1e3 == pytest.approx(
        96.4, rel=TOL
    )
    assert snap["component_mdot_kgs"]["feed"] == 0.0


def test_case_4_auto_selects_isentropic():
    """A vessel behind a permanently shut valve is integrated as isolated."""
    result = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0))
    assert any("integrated as isentropic" in e for e in result.events)


def test_case_5_coupled_through_solver():
    result = solve_transient(coupled_network(), o2_settings(), (0.0, 1.0))
    snap = result.at(1.0)

    assert psia_to_psig(snap["node_pressure_psia"]["acc"]) == pytest.approx(
        431.1, rel=TOL
    )
    assert snap["component_mdot_kgs"]["discharge"] * 1e3 == pytest.approx(
        119.0, rel=TOL
    )
    assert snap["component_mdot_kgs"]["feed"] > 0.0


def test_case_5_auto_selects_energy():
    result = solve_transient(coupled_network(), o2_settings(), (0.0, 1.0))
    assert any("integrated as energy" in e for e in result.events)


def test_explicit_isentropic_matches_auto_isolated():
    auto = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0)).at(1.0)
    forced = solve_transient(
        isolated_network(VesselModel.ISENTROPIC), o2_settings(), (0.0, 1.0)
    ).at(1.0)
    assert forced["node_pressure_psia"]["acc"] == pytest.approx(
        auto["node_pressure_psia"]["acc"], rel=1e-6
    )


def test_energy_model_reproduces_the_isolated_case():
    """Forcing the two-state form on an isolated vessel must give case 4."""
    result = solve_transient(
        isolated_network(VesselModel.ENERGY), o2_settings(), (0.0, 1.0)
    )
    snap = result.at(1.0)
    assert psia_to_psig(snap["node_pressure_psia"]["acc"]) == pytest.approx(
        331.4, rel=TOL
    )


def test_coupled_and_isolated_are_substantially_different():
    iso = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0)).at(1.0)
    cpl = solve_transient(coupled_network(), o2_settings(), (0.0, 1.0)).at(1.0)
    delta = cpl["node_pressure_psia"]["acc"] - iso["node_pressure_psia"]["acc"]
    assert delta > 50.0


# --- vessel physics --------------------------------------------------------


def test_vessel_cools_as_it_blows_down():
    result = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0))
    temps = result.node_temperatures_K["acc"]
    assert temps[0] == pytest.approx(294.44, rel=1e-3)
    assert temps[-1] < temps[0]
    assert np.all(np.diff(temps) <= 1e-9)  # monotonically cooling


def test_pressure_decreases_monotonically_when_isolated():
    result = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0))
    p = result.node_pressures_psia["acc"]
    assert np.all(np.diff(p) < 0.0)


def test_coupled_vessel_stays_warmer_than_isolated():
    iso = solve_transient(isolated_network(), o2_settings(), (0.0, 1.0))
    cpl = solve_transient(coupled_network(), o2_settings(), (0.0, 1.0))
    assert cpl.node_temperatures_K["acc"][-1] > iso.node_temperatures_K["acc"][-1]


# --- valve schedules -------------------------------------------------------


def test_scheduled_close_switches_coupled_to_isolated_behaviour():
    net = coupled_network()
    net.components["feed"].schedule = ValveSchedule(
        close_time=0.3, ramp_time=0.0, initially_open=True
    )
    result = solve_transient(net, o2_settings(), (0.0, 1.0))

    feed = result.component_mdot_kgs["feed"]
    # At t=0 the vessel sits at supply pressure, so inflow starts at exactly
    # zero and only builds as the vessel drains.
    before = feed[(result.t > 0.0) & (result.t < 0.25)]
    after = feed[result.t > 0.35]
    assert np.all(before > 0.0)
    assert np.all(after == 0.0)

    # Having lost its supply, it ends up below the fully-coupled case.
    coupled_end = solve_transient(coupled_network(), o2_settings(), (0.0, 1.0)).at(
        1.0
    )["node_pressure_psia"]["acc"]
    assert result.at(1.0)["node_pressure_psia"]["acc"] < coupled_end


def test_scheduled_open_admits_flow_partway_through():
    net = isolated_network()
    net.components["feed"].schedule = ValveSchedule(
        open_time=0.4, ramp_time=0.0, initially_open=False
    )
    result = solve_transient(net, o2_settings(), (0.0, 1.0))

    feed = result.component_mdot_kgs["feed"]
    assert np.all(feed[result.t < 0.35] == 0.0)
    assert np.any(feed[result.t > 0.45] > 0.0)
    # A schedulable valve means a supply path exists, so it must use the
    # energy formulation despite starting shut.
    assert any("integrated as energy" in e for e in result.events)


def test_valve_ramp_is_gradual():
    schedule = ValveSchedule(open_time=0.1, ramp_time=0.2, initially_open=False)
    assert schedule.fraction_open(0.0) == 0.0
    assert schedule.fraction_open(0.1) == pytest.approx(0.0)
    assert schedule.fraction_open(0.2) == pytest.approx(0.5)
    assert schedule.fraction_open(0.3) == pytest.approx(1.0)
    assert schedule.fraction_open(5.0) == pytest.approx(1.0)


# --- branching under transient conditions ---------------------------------


def test_accumulator_feeding_two_branches():
    net = isolated_network()
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Tee"))
    net.remove_component("discharge")
    net.add_component(_line(2.394, "acc", "tee", "main"))
    net.add_component(_line(0.732, "tee", "engine", "b1"))
    net.add_component(_line(0.366, "tee", "engine", "b2"))

    result = solve_transient(net, o2_settings(), (0.0, 0.5))
    snap = result.at(0.5)
    b1 = snap["component_mdot_kgs"]["b1"]
    b2 = snap["component_mdot_kgs"]["b2"]
    main = snap["component_mdot_kgs"]["main"]

    assert b1 + b2 == pytest.approx(main, rel=1e-5)
    assert b1 > b2  # uneven split, as the differing Cv requires


# --- errors ----------------------------------------------------------------


def test_transient_requires_a_dynamic_component():
    net = Network(name="static")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            name="Supply",
            supply_pressure_psig=500.0,
        )
    )
    net.add_node(_engine())
    net.add_component(_line(1.0, "supply", "engine", "line"))

    with pytest.raises(TransientError, match="dynamic state"):
        solve_transient(net, o2_settings(), (0.0, 1.0))
