"""Topology validation and the part library."""

from __future__ import annotations

import pytest

from pidsim.model import (
    ComponentType,
    FlowComponent,
    Network,
    NetworkError,
    Node,
    NodeKind,
    Severity,
    SinkSpec,
    get_part,
    part_library,
    parts_of_type,
    series_component,
)


def _line(cv: float, a: str, b: str, cid: str) -> FlowComponent:
    return FlowComponent(
        type=ComponentType.PIPE, from_node=a, to_node=b, id=cid, cv=cv, name=cid
    )


def minimal_network() -> Network:
    net = Network()
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            name="Supply",
            supply_pressure_psig=500.0,
        )
    )
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=150.0,
        )
    )
    net.add_component(_line(1.0, "supply", "engine", "line"))
    return net


def errors(net: Network) -> list[str]:
    return [i.message for i in net.validate() if i.severity is Severity.ERROR]


def test_minimal_network_is_valid():
    assert errors(minimal_network()) == []


def test_two_engines_are_rejected():
    net = minimal_network()
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine2",
            name="Second Engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=150.0,
        )
    )
    net.add_component(_line(1.0, "supply", "engine2", "line2"))
    assert any("exactly one engine" in m for m in errors(net))


def test_missing_engine_is_rejected():
    net = Network()
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            supply_pressure_psig=500.0,
        )
    )
    net.add_node(Node(kind=NodeKind.JUNCTION, id="j"))
    net.add_component(_line(1.0, "supply", "j", "line"))
    assert any("No engine/sink" in m for m in errors(net))


def test_missing_source_is_rejected():
    net = Network()
    net.add_node(Node(kind=NodeKind.JUNCTION, id="j"))
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=150.0,
        )
    )
    net.add_component(_line(1.0, "j", "engine", "line"))
    assert any("No source" in m for m in errors(net))


def test_unconnected_node_is_rejected():
    net = minimal_network()
    net.add_node(Node(kind=NodeKind.JUNCTION, id="orphan", name="Orphan"))
    assert any("not connected to anything" in m for m in errors(net))


def test_island_is_rejected():
    net = minimal_network()
    net.add_node(Node(kind=NodeKind.JUNCTION, id="a", name="Island A"))
    net.add_node(Node(kind=NodeKind.JUNCTION, id="b", name="Island B"))
    net.add_component(_line(1.0, "a", "b", "island_line"))
    assert any("Not connected to the source train" in m for m in errors(net))


def test_self_connection_is_rejected_at_construction():
    net = minimal_network()
    with pytest.raises(ValueError, match="to itself"):
        net.add_component(_line(1.0, "supply", "supply", "loop"))


def test_unknown_node_reference_is_rejected():
    net = minimal_network()
    with pytest.raises(ValueError, match="unknown node"):
        net.add_component(_line(1.0, "supply", "nowhere", "bad"))


def test_duplicate_ids_are_rejected():
    net = minimal_network()
    with pytest.raises(ValueError, match="Duplicate"):
        net.add_component(_line(1.0, "supply", "engine", "line"))


def test_accumulator_needs_volume_and_charge():
    net = minimal_network()
    net.add_node(Node(kind=NodeKind.ACCUMULATOR, id="acc", name="Acc"))
    net.add_component(_line(1.0, "supply", "acc", "feed"))
    messages = errors(net)
    assert any("no volume" in m for m in messages)
    assert any("no initial charge pressure" in m for m in messages)


def test_engine_needs_its_specified_boundary():
    net = minimal_network()
    net.nodes["engine"].sink_spec = SinkSpec.BOTH
    net.nodes["engine"].target_mdot_kgs = None
    assert any("target mass flow" in m for m in errors(net))


def test_raise_on_errors_raises():
    net = minimal_network()
    net.nodes["engine"].target_pressure_psig = None
    with pytest.raises(NetworkError, match="cannot be solved"):
        net.raise_on_errors()


def test_raise_on_errors_returns_non_errors():
    net = minimal_network()
    assert net.raise_on_errors() == []


def test_removing_a_node_removes_its_components():
    net = minimal_network()
    net.add_node(Node(kind=NodeKind.JUNCTION, id="j"))
    net.add_component(_line(1.0, "supply", "j", "extra"))
    net.remove_node("j")
    assert "extra" not in net.components
    assert "j" not in net.nodes


def test_loop_count_distinguishes_trees_from_merges():
    net = minimal_network()
    assert net.independent_loop_count("supply") == 0

    net.add_node(Node(kind=NodeKind.TEE, id="tee"))
    net.add_component(_line(1.0, "supply", "tee", "a"))
    net.add_component(_line(1.0, "tee", "engine", "b"))
    assert net.independent_loop_count("supply") == 1


# --- part library ----------------------------------------------------------


def test_library_loads_the_seeded_parts():
    lib = part_library()
    assert len(lib) >= 13
    assert lib["pipe_075_mm"].cv == 10.8
    assert lib["ss_hose_038"].cv == 1.7


def test_matheson_regulator_entry():
    reg = get_part("matheson_3200_ch4")
    assert reg.cv == 1.0
    assert reg.type is ComponentType.REGULATOR
    assert "CGA-350" in reg.inlet_size
    assert reg.outlet_max_psig == 250.0


def test_unknown_part_lists_alternatives():
    with pytest.raises(KeyError, match="Unknown part"):
        get_part("not_a_real_part")


def test_parts_can_be_filtered_by_type():
    adapters = parts_of_type(ComponentType.ADAPTER)
    assert {p.cv for p in adapters} == {10.8, 7.1, 1.7}


def test_instantiating_a_part_carries_its_specs():
    comp = get_part("solenoid_075").instantiate("a", "b")
    assert comp.cv == 9.3
    assert comp.type is ComponentType.SOLENOID_VALVE
    assert comp.library_key == "solenoid_075"
    assert comp.from_node == "a" and comp.to_node == "b"


def test_series_component_reproduces_the_branch_cv():
    """The seeded 3/8 chain collapses to the reference case's Cv=0.732."""
    parts = [get_part("reducer_075m_038f")] + [
        get_part(k)
        for k in ("npt_jic_038", "jic_hose_038", "ss_hose_038", "jic_hose_038",
                  "npt_jic_038")
    ]
    comps = [p.instantiate("a", "b") for p in parts]
    lumped = series_component(comps, "a", "b", name="3/8 branch")
    assert lumped.cv == pytest.approx(0.732, rel=0.01)
