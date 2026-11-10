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


# --- junction temperature propagation ---------------------------------------
#
# A plain junction has no thermal state of its own; its temperature should be
# the mass-weighted average of whatever is flowing into it right now, not a
# flat project default. These tests build a vessel -> junction -> engine
# chain (not in any reference case, which all go vessel -> engine directly)
# specifically to exercise the gap this fixes.


def junction_network(model: VesselModel = VesselModel.AUTO) -> Network:
    """Isolated accumulator discharging through an intermediate junction."""
    net = isolated_network(model)
    net.add_node(Node(kind=NodeKind.JUNCTION, id="mid", name="Midpoint"))
    net.remove_component("discharge")
    # Generous Cv: negligible pressure drop, so this test isolates the
    # temperature question from any pressure-drop complication.
    net.add_component(_line(5.0, "acc", "mid", "leg1"))
    net.add_component(_line(0.732, "mid", "engine", "leg2"))
    return net


def test_junction_temperature_tracks_a_single_cooling_inflow_exactly():
    """One inflow: the mass-weighted average is exactly that inflow's value,
    not an approximation -- and it must actually move, not sit flat."""
    result = solve_transient(junction_network(), o2_settings(), (0.0, 1.0))
    acc_temp = result.node_temperatures_K["acc"]
    mid_temp = result.node_temperatures_K["mid"]

    np.testing.assert_allclose(mid_temp, acc_temp, rtol=1e-9)
    assert mid_temp[0] == pytest.approx(294.44, abs=0.1)  # 530 degR at t=0
    # Case 4's isentropic relation predicts ~31 K of cooling by t=1s
    # (T2 = T1*(P2/P1)**0.2857 with the reference case's own numbers); this
    # just needs to be clearly more than zero, not flat.
    assert mid_temp[-1] < mid_temp[0] - 20.0


def test_junction_temperature_matches_reference_case_4_through_an_extra_hop():
    """Same vessel/discharge Cv as reference case 4, just split across two
    components with a junction in between. The junction's pressure and
    temperature at t=1s should match case 4's accumulator state directly,
    since the inserted leg1 is sized to be nearly lossless."""
    result = solve_transient(junction_network(), o2_settings(), (0.0, 1.0))
    snap = result.at(1.0)
    assert psia_to_psig(snap["node_pressure_psia"]["mid"]) == pytest.approx(
        331.4, rel=TOL
    )
    assert snap["node_temperature_K"]["mid"] == pytest.approx(
        snap["node_temperature_K"]["acc"], rel=1e-6
    )


def test_downstream_mass_flow_actually_uses_the_propagated_temperature():
    """Not just a reporting change: prove the Cv equation for the component
    *past* the junction is evaluated at the junction's real (cooled)
    temperature, not the flat project default -- by cross-checking the
    solver's own answer against the physics function directly, and showing
    the project-default answer would have been measurably different."""
    from pidsim.physics.flow import mass_flow
    from pidsim.physics.gases import get_gas
    from pidsim.units import kelvin_to_rankine

    result = solve_transient(junction_network(), o2_settings(), (0.0, 1.0))
    snap = result.at(1.0)

    mid_p = snap["node_pressure_psia"]["mid"]
    mid_t_R = kelvin_to_rankine(snap["node_temperature_K"]["mid"])
    engine_p = snap["node_pressure_psia"]["engine"]
    actual_mdot = snap["component_mdot_kgs"]["leg2"]

    gas = get_gas("O2")
    correct = mass_flow(0.732, mid_p, engine_p, gas, mid_t_R, 530.0)
    naive_at_project_default = mass_flow(0.732, mid_p, engine_p, gas, 530.0, 530.0)

    # Within ~0.5%, not exact: the Cv equation that produced actual_mdot used
    # the *previous* evaluation's junction temperature (the one-evaluation
    # lag documented in transient.py), while `correct` uses this exact
    # sample's freshly propagated temperature. The gap this leaves is tiny
    # (observed ~0.01%) -- which is itself evidence the lag is negligible,
    # exactly as that design relies on.
    assert actual_mdot == pytest.approx(correct, rel=5e-3)
    # The junction has genuinely cooled by t=1s, so using the project default
    # instead would have given a measurably different (and wrong) answer --
    # two orders of magnitude bigger than the lag residual above.
    assert actual_mdot != pytest.approx(naive_at_project_default, rel=0.02)


def test_sink_temperature_reflects_the_gas_actually_arriving():
    """The engine's own reported temperature should track the cooled gas
    reaching it, not sit flat at project default -- new information this fix
    makes available, not just a junction-specific detail."""
    result = solve_transient(junction_network(), o2_settings(), (0.0, 1.0))
    engine_temp = result.node_temperatures_K["engine"]
    assert engine_temp[0] == pytest.approx(294.44, abs=0.1)
    assert engine_temp[-1] < engine_temp[0] - 20.0


def test_junction_mixes_two_inflows_at_different_temperatures():
    """A junction fed both by a bypass straight from the (constant-
    temperature) source and by the accumulator's (cooled) discharge should
    report the mass-weighted average of the two -- strictly between them,
    not equal to either -- within a single branching/merging source train."""
    net = coupled_network()
    net.add_node(Node(kind=NodeKind.JUNCTION, id="mid", name="Midpoint"))
    net.remove_component("discharge")
    net.add_component(_line(0.732, "acc", "mid", "acc_leg"))
    net.add_component(_line(0.1, "supply", "mid", "bypass"))
    net.add_component(_line(5.0, "mid", "engine", "outlet"))

    result = solve_transient(net, o2_settings(), (0.0, 1.0))
    snap = result.at(1.0)

    acc_t = snap["node_temperature_K"]["acc"]
    mid_t = snap["node_temperature_K"]["mid"]
    source_t = 294.44  # 530 degR, the supply's own (unchanging) temperature

    acc_mdot = snap["component_mdot_kgs"]["acc_leg"]
    bypass_mdot = snap["component_mdot_kgs"]["bypass"]
    assert acc_mdot > 0.0 and bypass_mdot > 0.0  # both legs genuinely flowing

    # Strictly between the two inflow temperatures, not equal to either --
    # proof this is a real mix, not one branch dominating or being ignored.
    assert acc_t < mid_t < source_t

    expected = (acc_mdot * acc_t + bypass_mdot * source_t) / (acc_mdot + bypass_mdot)
    assert mid_t == pytest.approx(expected, rel=1e-3)


def test_source_with_a_nondefault_temperature_is_reported_correctly():
    """A source colder than the project flowing temperature should show up
    as itself in the results, not silently fall back to the project default
    -- the same underlying gap, just for a source rather than a junction."""
    net = isolated_network()
    net.nodes["supply"].supply_temperature_R = 450.0  # colder than project's 530

    result = solve_transient(net, o2_settings(), (0.0, 0.1))
    from pidsim.units import rankine_to_kelvin

    assert result.node_temperatures_K["supply"][0] == pytest.approx(
        rankine_to_kelvin(450.0), rel=1e-6
    )


def test_branches_off_a_junction_both_see_its_propagated_temperature():
    """Extends the existing two-branch split test: both branches leaving the
    tee should agree with the tee's own reported temperature, since they
    share the same upstream node."""
    net = isolated_network()
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Tee"))
    net.remove_component("discharge")
    net.add_component(_line(2.394, "acc", "tee", "main"))
    net.add_component(_line(0.732, "tee", "engine", "b1"))
    net.add_component(_line(0.366, "tee", "engine", "b2"))

    result = solve_transient(net, o2_settings(), (0.0, 0.5))
    snap = result.at(0.5)

    tee_t = snap["node_temperature_K"]["tee"]
    assert tee_t == pytest.approx(snap["node_temperature_K"]["acc"], rel=1e-6)
    assert tee_t < 294.44 - 10.0  # genuinely cooled, not flat default


# --- stopping early once a vessel reaches equilibrium ----------------------
#
# Without this, a vessel approaching its own backpressure forces solve_ivp to
# resolve an ever-flatter asymptotic tail under a tight rtol, which can cost
# tens of thousands of extra integration steps for runs that explore far past
# where anything practically interesting still happens. Networks here are
# deliberately tiny and fast -- the scaling case that motivated this (a
# 100+ component ladder network taking 5+ minutes) is recorded with real
# numbers in docs/DEVELOPMENT-NOTES.md section 16, not re-run here.


def near_equilibrium_network(charge_psig: float = 500.0, target_psig: float = 480.0):
    """A small pressure gap (20 psi) relative to a 10 L vessel's own cv=0.732
    discharge settles out quickly -- built specifically to reach equilibrium
    well within a generously long requested time span."""
    net = isolated_network()
    net.nodes["engine"].target_pressure_psig = target_psig
    return net


def test_equilibrium_stop_terminates_a_run_early_at_the_right_pressure():
    net = near_equilibrium_network()
    result = solve_transient(net, o2_settings(), (0.0, 1000.0), n_output=11)

    # Requested 1000 s; a vessel discharging into a backpressure only 20 psi
    # below its own charge reaches that backpressure almost immediately, so
    # the run must end far short of the requested span.
    assert result.t[-1] < 10.0
    assert psia_to_psig(result.at(result.t[-1])["node_pressure_psia"]["acc"]) == pytest.approx(
        480.0, abs=0.5
    )
    assert any("effectively at steady state" in e for e in result.events)
    assert not any("ambient pressure" in e for e in result.events)


def test_equilibrium_stop_can_be_disabled():
    """Explicitly opting out must reproduce the old unconditional behaviour:
    keep integrating for the full requested span regardless of how early
    equilibrium was reached."""
    net = near_equilibrium_network()
    result = solve_transient(
        net, o2_settings(), (0.0, 2.0), n_output=11, stop_at_equilibrium=False
    )
    assert result.t[-1] == pytest.approx(2.0)
    assert not any("effectively at steady state" in e for e in result.events)


def test_equilibrium_stop_is_skipped_with_a_pending_valve_schedule():
    """A scheduled valve change later in the run means a temporary flow lull
    now must not be mistaken for true equilibrium and cut the run short --
    the gate should suppress the event entirely whenever one is pending."""
    net = near_equilibrium_network()
    net.components["feed"].schedule = ValveSchedule(open_time=5.0, initially_open=False)

    result = solve_transient(net, o2_settings(), (0.0, 10.0), n_output=11)
    assert result.t[-1] == pytest.approx(10.0)
    assert not any("effectively at steady state" in e for e in result.events)


def test_equilibrium_stop_uses_net_not_gross_flow_for_a_coupled_vessel():
    """A continuously-fed vessel can settle at a steady, large, *nonzero*
    throughput once inflow balances outflow -- gross flow staying large must
    not prevent the stop; only the net flow that actually drives dP/dt (or
    dm/dt) should be checked, the same quantity rhs() already uses."""
    net = coupled_network()
    net.nodes["engine"].target_pressure_psig = 490.0  # close to the 500 psig charge

    result = solve_transient(net, o2_settings(), (0.0, 1000.0), n_output=11)
    # Two state variables (mass and energy) both have to settle, not just
    # one, so this genuinely takes longer than the isentropic case above --
    # the only thing that matters here is that it stops well short of the
    # requested 1000 s span, not a specific fast time.
    assert result.t[-1] < 200.0

    snap = result.at(result.t[-1])
    feed_mdot = snap["component_mdot_kgs"]["feed"]
    discharge_mdot = snap["component_mdot_kgs"]["discharge"]
    # Gross throughput is substantial -- this genuinely is the coupled,
    # still-flowing case, not a trivial zero-flow shutdown.
    assert feed_mdot > 0.01
    assert discharge_mdot == pytest.approx(feed_mdot, rel=0.05)
    assert any("effectively at steady state" in e for e in result.events)


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
