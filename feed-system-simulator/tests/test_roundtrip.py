"""Save/load round-trips and result export."""

from __future__ import annotations

import json

import pytest

from pidsim.model import (
    ComponentType,
    FlowComponent,
    Network,
    Node,
    NodeKind,
    Project,
    ProjectSettings,
    SinkSpec,
    ValveSchedule,
    VesselModel,
    load,
    save,
    to_dict,
    from_dict,
)
from pidsim.solver.steady import SolveMode, solve_steady
from pidsim.solver.transient import solve_transient
from pidsim.units import LITER_TO_M3


def build_project() -> Project:
    net = Network(name="O2 train")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            name="Regulator Outlet",
            supply_pressure_psig=458.5,
            x=10.0,
            y=20.0,
        )
    )
    net.add_node(
        Node(
            kind=NodeKind.ACCUMULATOR,
            id="acc",
            name="Accumulator",
            volume_m3=10.0 * LITER_TO_M3,
            charge_pressure_psig=500.0,
            charge_temperature_R=530.0,
            vessel_model=VesselModel.ENERGY,
            x=120.0,
            y=20.0,
        )
    )
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Branch Tee", x=220.0, y=20.0))
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.BOTH,
            target_mdot_kgs=0.220,
            target_pressure_psig=150.0,
            x=340.0,
            y=20.0,
        )
    )
    net.add_component(
        FlowComponent(
            type=ComponentType.BALL_VALVE,
            from_node="supply",
            to_node="acc",
            id="feed",
            name="Isolation Valve",
            cv=0.923,
            manufacturer="Swagelok",
            part_number="SS-65TS12",
            max_pressure_psig=3000.0,
            schedule=ValveSchedule(close_time=0.5, ramp_time=0.05),
        )
    )
    net.add_component(
        FlowComponent(
            type=ComponentType.PIPE,
            from_node="acc",
            to_node="tee",
            id="main",
            name="Main Chain",
            cv=2.394,
        )
    )
    for cid in ("b1", "b2"):
        net.add_component(
            FlowComponent(
                type=ComponentType.PIPE,
                from_node="tee",
                to_node="engine",
                id=cid,
                name=cid,
                cv=0.732,
            )
        )
    return Project(
        settings=ProjectSettings(gas_name="O2", t_flow_R=530.0, t_ref_R=530.0),
        network=net,
        name="RDE O2 feed",
        notes="Round-trip fixture",
    )


def test_dict_round_trip_preserves_everything():
    original = build_project()
    restored = from_dict(to_dict(original))

    assert restored.name == original.name
    assert restored.notes == original.notes
    assert restored.settings.gas_name == "O2"
    assert restored.settings.t_ref_R == 530.0
    assert set(restored.network.nodes) == set(original.network.nodes)
    assert set(restored.network.components) == set(original.network.components)

    acc = restored.network.nodes["acc"]
    assert acc.kind is NodeKind.ACCUMULATOR
    assert acc.vessel_model is VesselModel.ENERGY
    assert acc.volume_m3 == pytest.approx(0.010)
    assert acc.x == 120.0

    feed = restored.network.components["feed"]
    assert feed.type is ComponentType.BALL_VALVE
    assert feed.part_number == "SS-65TS12"
    assert feed.schedule is not None
    assert feed.schedule.close_time == 0.5
    assert feed.schedule.ramp_time == 0.05


def test_file_round_trip(tmp_path):
    original = build_project()
    path = save(original, tmp_path / "design.json")
    restored = load(path)
    assert to_dict(restored) == to_dict(original)


def test_saved_file_is_readable_json(tmp_path):
    path = save(build_project(), tmp_path / "design.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["network"]["name"] == "O2 train"


def test_round_trip_gives_identical_steady_results(tmp_path):
    original = build_project()
    # Open the valve so the steady solve sees a complete path.
    original.network.components["feed"].schedule = None
    before = solve_steady(
        original.network, original.settings, SolveMode.BACKWARD
    )

    restored = load(save(original, tmp_path / "design.json"))
    after = solve_steady(restored.network, restored.settings, SolveMode.BACKWARD)

    assert after.solved_boundary_psia == pytest.approx(before.solved_boundary_psia)
    for cid, flow in before.component_flows.items():
        assert after.component_flows[cid].mdot_kgs == pytest.approx(flow.mdot_kgs)


def test_round_trip_gives_identical_transient_results(tmp_path):
    original = build_project()
    before = solve_transient(original.network, original.settings, (0.0, 0.5))

    restored = load(save(original, tmp_path / "design.json"))
    after = solve_transient(restored.network, restored.settings, (0.0, 0.5))

    assert after.at(0.5)["node_pressure_psia"]["acc"] == pytest.approx(
        before.at(0.5)["node_pressure_psia"]["acc"]
    )


def test_newer_file_version_is_rejected():
    data = to_dict(build_project())
    data["version"] = 999
    with pytest.raises(ValueError, match="newer than this build"):
        from_dict(data)


def test_unknown_fields_are_ignored_for_forward_compatibility():
    data = to_dict(build_project())
    data["network"]["nodes"][0]["some_future_field"] = 42
    data["settings"]["another_future_field"] = "x"
    restored = from_dict(data)
    assert "supply" in restored.network.nodes


# --- result export ---------------------------------------------------------


def test_steady_result_csv_export(tmp_path):
    project = build_project()
    project.network.components["feed"].schedule = None
    result = solve_steady(project.network, project.settings, SolveMode.BACKWARD)

    path = result.to_csv(tmp_path / "steady.csv")
    text = path.read_text(encoding="utf-8")
    assert "pressure_psia" in text
    assert "Main Chain" in text
    assert "choked" in text or "subsonic" in text


def test_transient_result_csv_export(tmp_path):
    project = build_project()
    result = solve_transient(project.network, project.settings, (0.0, 0.2))

    path = result.to_csv(tmp_path / "transient.csv")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("t_s")
    assert "P_psia[Accumulator]" in lines[0]
    assert "mdot_g_s[Main Chain]" in lines[0]
    assert len(lines) == len(result.t) + 1
