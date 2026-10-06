"""Headless GUI smoke tests.

These run Qt with the offscreen platform plugin, so they exercise the real
widgets without needing a display. They check wiring -- that the canvas builds
items from a model, that edits write through, and that a solve annotates the
diagram -- not pixels.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pidsim.gui.canvas import PidScene, default_node  # noqa: E402
from pidsim.gui.examples import EXAMPLES, accumulator_blowdown, o2_two_branch  # noqa: E402
from pidsim.gui.properties import PropertyPanel  # noqa: E402
from pidsim.model.components import ComponentType, NodeKind  # noqa: E402
from pidsim.solver.steady import SolveMode, solve_steady  # noqa: E402
from pidsim.solver.transient import solve_transient  # noqa: E402
from pidsim.units import psia_to_psig  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --- examples --------------------------------------------------------------


def test_every_example_is_valid_and_solvable():
    for name, factory in EXAMPLES.items():
        project = factory()
        errors = [
            i for i in project.network.validate() if i.severity.value == "error"
        ]
        assert errors == [], f"{name}: {[e.message for e in errors]}"


def test_o2_example_reproduces_case_2():
    project = o2_two_branch()
    result = solve_steady(project.network, project.settings, SolveMode.BACKWARD)
    assert psia_to_psig(result.solved_boundary_psia) == pytest.approx(458.5, rel=0.01)


def test_ch4_example_reproduces_case_3():
    project = EXAMPLES["CH4 regulator chain (case 3)"]()
    result = solve_steady(
        project.network, project.settings, SolveMode.BACKWARD, solve_for="reg"
    )
    assert psia_to_psig(result.solved_boundary_psia) == pytest.approx(182.5, rel=0.01)


def test_accumulator_example_reproduces_cases_4_and_5():
    project = accumulator_blowdown()

    coupled = solve_transient(project.network, project.settings, (0.0, 1.0))
    assert psia_to_psig(coupled.at(1.0)["node_pressure_psia"]["acc"]) == pytest.approx(
        431.1, rel=0.01
    )

    project.network.components["feed"].is_open = False
    isolated = solve_transient(project.network, project.settings, (0.0, 1.0))
    assert psia_to_psig(
        isolated.at(1.0)["node_pressure_psia"]["acc"]
    ) == pytest.approx(331.4, rel=0.01)


# --- canvas ----------------------------------------------------------------


def test_scene_builds_items_for_every_model_object(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    assert set(scene.node_items) == set(project.network.nodes)
    assert set(scene.edge_items) == set(project.network.components)


def test_moving_a_node_item_updates_the_model(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    item = scene.node_items["tee"]
    item.setPos(123.0, -45.0)
    assert project.network.nodes["tee"].x == 123.0
    assert project.network.nodes["tee"].y == -45.0


def test_adding_a_node_writes_through_to_the_network(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)
    before = len(project.network.nodes)

    item = scene.add_node(NodeKind.ACCUMULATOR, QPointF(10.0, 20.0))
    assert len(project.network.nodes) == before + 1
    assert project.network.nodes[item.node_id].kind is NodeKind.ACCUMULATOR
    assert project.network.nodes[item.node_id].volume_m3 > 0


def test_connecting_nodes_creates_a_component(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    new_node = scene.add_node(NodeKind.JUNCTION, QPointF(0.0, 200.0))
    edge = scene.connect_nodes("tee", new_node.node_id)

    assert edge is not None
    assert edge.component_id in project.network.components
    assert edge.component_id in scene.edge_items


def test_connecting_a_node_to_itself_is_refused(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)
    messages: list[str] = []
    scene.status_message.connect(messages.append)

    assert scene.connect_nodes("tee", "tee") is None
    assert any("itself" in m for m in messages)


def test_deleting_a_node_removes_its_edges(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    scene.clearSelection()
    scene.node_items["tee"].setSelected(True)
    scene.delete_selection()

    assert "tee" not in project.network.nodes
    assert "main" not in project.network.components
    assert "branch_a" not in project.network.components
    assert scene.edge_items == {}


def test_steady_results_annotate_the_canvas(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    result = solve_steady(project.network, project.settings, SolveMode.BACKWARD)
    scene.show_steady_result(result)

    assert scene.node_items["tee"].result_lines
    assert "psig" in scene.node_items["tee"].result_lines[0]
    assert scene.edge_items["branch_a"].choked is True
    assert "g/s" in scene.edge_items["branch_a"].result_text

    scene.clear_results()
    assert scene.node_items["tee"].result_lines == []
    assert scene.edge_items["branch_a"].result_text == ""


def test_transient_snapshot_annotates_the_canvas(qapp):
    project = accumulator_blowdown()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    result = solve_transient(project.network, project.settings, (0.0, 1.0))
    scene.show_transient_snapshot(result, 1.0)

    lines = scene.node_items["acc"].result_lines
    assert any("psig" in line for line in lines)
    assert any("K" in line for line in lines)


def test_ports_reflect_node_direction(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    source = scene.node_items["reg_out"]
    assert source.inlet_ports == []  # a supply has nothing feeding it
    assert len(source.outlet_ports) == 1

    engine = scene.node_items["engine"]
    assert engine.outlet_ports == []  # the engine is the end of the line
    assert len(engine.inlet_ports) == 3


def test_tee_and_engine_have_three_connection_points(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    assert len(scene.node_items["tee"].ports) == 3
    assert len(scene.node_items["engine"].ports) == 3
    # A tee leg is bidirectional: which one is the inlet is a plumbing choice.
    assert all(p.role == "both" for p in scene.node_items["tee"].ports)
    assert all(p.role == "in" for p in scene.node_items["engine"].ports)


def test_two_port_kinds_are_unchanged(qapp):
    project = accumulator_blowdown()
    scene = PidScene(project.network)
    scene.rebuild(project.network)
    assert len(scene.node_items["acc"].ports) == 2


def test_lines_at_a_tee_take_distinct_legs(qapp):
    """Main in, two branches out -- one line per leg, none doubled up."""
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    tee = scene.node_items["tee"]
    used = {
        scene.edge_items["main"].dst_port,
        scene.edge_items["branch_a"].src_port,
        scene.edge_items["branch_b"].src_port,
    }
    assert len(used) == 3
    assert used == set(tee.ports)


def test_branches_reach_the_engine_on_separate_inlets(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    a = scene.edge_items["branch_a"].dst_port
    b = scene.edge_items["branch_b"].dst_port
    assert a is not None and b is not None
    assert a is not b


def test_parallel_branches_do_not_cross(qapp):
    """The line off the tee's bottom leg must land on the engine's lower inlet.

    Both branches join the same pair of nodes, so a centre-to-centre heuristic
    alone cannot order them and they cross. Assignment refines against the far
    end's chosen anchor to keep them parallel.
    """
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    for cid in ("branch_a", "branch_b"):
        edge = scene.edge_items[cid]
        if edge.src_port.name == "branch":  # leaves the tee downwards
            assert edge.dst_port.name == "lower"
        elif edge.src_port.name == "east":  # leaves level
            assert edge.dst_port.name == "middle"


def test_port_assignment_reaches_a_fixed_point(qapp):
    """Repeated passes must settle, not flip back and forth during a drag."""
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    def snapshot():
        return {
            cid: (e.src_port.name, e.dst_port.name)
            for cid, e in scene.edge_items.items()
        }

    scene.reassign_all_ports()
    settled = snapshot()
    for _ in range(8):
        scene.reassign_all_ports()
        assert snapshot() == settled


def test_ports_follow_the_geometry_when_a_node_moves(qapp):
    """Drag a branch below the tee and its line should take the branch leg."""
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    tee = scene.node_items["tee"]
    engine = scene.node_items["engine"]

    def legs_leaving_tee() -> set[str]:
        return {
            scene.edge_items[c].src_port.name for c in ("branch_a", "branch_b")
        }

    # Engine directly below: one branch should take the tee's bottom leg.
    engine.setPos(tee.pos().x(), tee.pos().y() + 400.0)
    assert "branch" in legs_leaving_tee()

    # Engine far to the right: the level leg becomes the natural one.
    engine.setPos(tee.pos().x() + 600.0, tee.pos().y())
    assert "east" in legs_leaving_tee()

    # Two branches cannot share one leg.
    assert len(legs_leaving_tee()) == 2


# --- rotation --------------------------------------------------------------


def test_rotating_a_node_moves_its_legs(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    tee = scene.node_items["tee"]
    west = next(p for p in tee.ports if p.name == "west")
    assert west.pos().x() < 0 and abs(west.pos().y()) < 1e-6

    tee.rotate_by(90.0)

    # A quarter turn clockwise takes the west leg to the top.
    assert abs(west.pos().x()) < 1e-6
    assert west.pos().y() < 0
    assert project.network.nodes["tee"].rotation == 90.0


def test_rotation_wraps_after_four_quarter_turns(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)
    tee = scene.node_items["tee"]

    before = {p.name: (p.pos().x(), p.pos().y()) for p in tee.ports}
    for _ in range(4):
        tee.rotate_by(90.0)

    assert tee.rotation_deg == 0.0
    for port in tee.ports:
        x, y = before[port.name]
        assert port.pos().x() == pytest.approx(x, abs=1e-6)
        assert port.pos().y() == pytest.approx(y, abs=1e-6)


def test_rotation_swaps_the_on_screen_footprint(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)
    tee = scene.node_items["tee"]

    upright = tee._body_size()
    tee.rotate_by(90.0)
    assert tee._body_size() == (upright[1], upright[0])


def test_rotate_selection_only_affects_selected_nodes(qapp):
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    scene.clearSelection()
    scene.node_items["tee"].setSelected(True)
    scene.rotate_selection(90.0)

    assert project.network.nodes["tee"].rotation == 90.0
    assert project.network.nodes["engine"].rotation == 0.0


def test_rotation_does_not_change_the_solution(qapp):
    """Rotation is presentation only; the solver must not see it."""
    project = o2_two_branch()
    before = solve_steady(project.network, project.settings, SolveMode.BACKWARD)

    scene = PidScene(project.network)
    scene.rebuild(project.network)
    for node_id, angle in (("tee", 90.0), ("engine", 180.0), ("reg_out", 270.0)):
        scene.node_items[node_id].rotate_by(angle)

    after = solve_steady(project.network, project.settings, SolveMode.BACKWARD)
    assert after.solved_boundary_psia == pytest.approx(before.solved_boundary_psia)
    for cid, flow in before.component_flows.items():
        assert after.component_flows[cid].mdot_kgs == pytest.approx(flow.mdot_kgs)


def test_rotation_survives_save_and_load(qapp, tmp_path):
    from pidsim.model import load, save

    project = o2_two_branch()
    project.network.nodes["tee"].rotation = 270.0
    restored = load(save(project, tmp_path / "rotated.json"))
    assert restored.network.nodes["tee"].rotation == 270.0

    scene = PidScene(restored.network)
    scene.rebuild(restored.network)
    west = next(p for p in scene.node_items["tee"].ports if p.name == "west")
    assert west.pos().y() > 0  # 270 degrees puts the west leg at the bottom


def test_lines_follow_a_rotated_tee(qapp):
    """Turn the tee and the branch leg should still be the one used."""
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    scene.node_items["tee"].rotate_by(90.0)
    legs = {
        scene.edge_items[c].src_port.name for c in ("branch_a", "branch_b")
    }
    assert len(legs) == 2
    for cid in ("main", "branch_a", "branch_b"):
        edge = scene.edge_items[cid]
        assert edge.src_port is not None and edge.dst_port is not None


def test_more_lines_than_legs_still_works(qapp):
    """The model allows any number of components per node; legs are shared."""
    project = o2_two_branch()
    scene = PidScene(project.network)
    scene.rebuild(project.network)

    for i in range(4):
        extra = scene.add_node(NodeKind.JUNCTION, QPointF(0.0, 150.0 * (i + 1)))
        assert scene.connect_nodes("tee", extra.node_id) is not None

    # Seven lines meet at a three-legged tee: every one still has an anchor.
    assert len(project.network.components_at("tee")) == 7
    for comp in project.network.components_at("tee"):
        edge = scene.edge_items[comp.id]
        assert (edge.src_port is not None) or (edge.dst_port is not None)


def test_default_nodes_are_populated():
    for kind in NodeKind:
        node = default_node(kind)
        assert node.name
        if kind is NodeKind.ACCUMULATOR:
            assert node.volume_m3 and node.charge_pressure_psig
        if kind is NodeKind.SINK:
            assert node.target_pressure_psig and node.target_mdot_kgs


# --- property editor -------------------------------------------------------


def test_property_panel_edits_write_through_to_the_model(qapp):
    project = o2_two_branch()
    panel = PropertyPanel()

    comp = project.network.components["main"]
    panel.show_object(comp)
    changed: list[str] = []
    panel.changed.connect(changed.append)

    # Find the Cv spin box and drive it the way a user would.
    from PySide6.QtWidgets import QDoubleSpinBox

    spins = panel.findChildren(QDoubleSpinBox)
    cv_box = next(s for s in spins if abs(s.value() - 2.394) < 1e-9)
    cv_box.setValue(3.5)

    assert comp.cv == pytest.approx(3.5)
    assert changed


def test_property_panel_handles_every_node_kind(qapp):
    project = accumulator_blowdown()
    panel = PropertyPanel()
    for node in project.network.nodes.values():
        panel.show_object(node)  # must not raise
    for comp in project.network.components.values():
        panel.show_object(comp)
    panel.show_object(None)


def test_selecting_a_catalogue_part_fills_the_component(qapp):
    project = o2_two_branch()
    panel = PropertyPanel()
    comp = project.network.components["main"]
    panel.show_object(comp)

    from PySide6.QtWidgets import QComboBox

    library = panel.findChildren(QComboBox)[0]
    index = library.findData("solenoid_075")
    assert index >= 0
    library.setCurrentIndex(index)

    assert comp.cv == pytest.approx(9.3)
    assert comp.type is ComponentType.SOLENOID_VALVE
    assert comp.library_key == "solenoid_075"
