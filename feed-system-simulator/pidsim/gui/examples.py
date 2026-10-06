"""Worked examples, so the app has something real to show on first run.

These are the validated reference systems: the numbers they produce are the
ones in ``tests/test_validation.py``.
"""

from __future__ import annotations

from ..model.components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
    VesselModel,
)
from ..model.network import Network
from ..model.project import Project, ProjectSettings
from ..units import LITER_TO_M3


def _line(cv: float, a: str, b: str, cid: str, name: str, **kw) -> FlowComponent:
    return FlowComponent(
        type=kw.pop("type", ComponentType.PIPE),
        from_node=a,
        to_node=b,
        id=cid,
        cv=cv,
        name=name,
        **kw,
    )


def o2_two_branch() -> Project:
    """Reference cases 1 and 2: regulator -> main chain -> tee -> two branches.

    Backward mode gives a required regulator outlet of ~458.5 psig, with the
    tee at ~403.5 psig and both branches choked.
    """
    net = Network(name="O2 feed -- two branches")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="reg_out",
            name="Regulator Outlet",
            supply_pressure_psig=458.5,
            x=-320.0,
            y=0.0,
        )
    )
    net.add_node(Node(kind=NodeKind.TEE, id="tee", name="Branch Tee", x=-100.0, y=0.0))
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.BOTH,
            target_mdot_kgs=0.220,
            target_pressure_psig=150.0,
            x=160.0,
            y=0.0,
        )
    )
    net.add_component(
        _line(2.394, "reg_out", "tee", "main", "3/4\" Main Chain")
    )
    net.add_component(_line(0.732, "tee", "engine", "branch_a", "3/8\" Branch A"))
    net.add_component(_line(0.732, "tee", "engine", "branch_b", "3/8\" Branch B"))
    net.nodes["tee"].y = 0.0

    return Project(
        settings=ProjectSettings(gas_name="O2", t_flow_R=530.0, t_ref_R=530.0),
        network=net,
        name="O2 two-branch feed",
        notes=(
            "Validated reference cases 1 and 2. Run Backward Steady: the "
            "regulator outlet should come out at ~458.5 psig and the tee at "
            "~403.5 psig, with both branches choked."
        ),
    )


def ch4_chain() -> Project:
    """Reference case 3: a single CH4 chain of Cv 1.883 at 60 g/s."""
    net = Network(name="CH4 feed")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="bottle",
            name="CH4 Bottle",
            supply_pressure_psig=2000.0,
            x=-360.0,
            y=0.0,
        )
    )
    net.add_node(
        Node(kind=NodeKind.JUNCTION, id="reg_out", name="Regulator Outlet", x=-140.0, y=0.0)
    )
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.BOTH,
            target_mdot_kgs=0.060,
            target_pressure_psig=150.0,
            x=140.0,
            y=0.0,
        )
    )
    net.add_component(
        _line(
            1.0,
            "bottle",
            "reg_out",
            "reg",
            "Matheson 3200/3240",
            type=ComponentType.REGULATOR,
            manufacturer="Matheson",
            part_number="3200/3240",
            library_key="matheson_3200_ch4",
            inlet_size="CGA-350",
            outlet_max_psig=250.0,
        )
    )
    net.add_component(_line(1.883, "reg_out", "engine", "chain", "CH4 Chain"))

    return Project(
        settings=ProjectSettings(gas_name="CH4", t_flow_R=530.0, t_ref_R=530.0),
        network=net,
        name="CH4 feed chain",
        notes=(
            "Validated reference case 3. The regulator has no setpoint, so "
            "Run -> Size regulator solves for the outlet it needs: ~182.5 psig."
        ),
    )


def accumulator_blowdown() -> Project:
    """Reference cases 4 and 5: a 10 L accumulator into a 150 psig manifold.

    With the isolation valve shut it is case 4 (331.4 psig, 96.4 g/s at 1 s);
    open, it is case 5 (431.1 psig, 119 g/s).
    """
    net = Network(name="Accumulator blowdown")
    net.add_node(
        Node(
            kind=NodeKind.SOURCE,
            id="supply",
            name="Supply",
            supply_pressure_psig=500.0,
            x=-320.0,
            y=0.0,
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
            vessel_model=VesselModel.AUTO,
            x=-80.0,
            y=0.0,
        )
    )
    net.add_node(
        Node(
            kind=NodeKind.SINK,
            id="engine",
            name="Engine",
            sink_spec=SinkSpec.PRESSURE,
            target_pressure_psig=150.0,
            x=180.0,
            y=0.0,
        )
    )
    net.add_component(
        _line(
            0.923,
            "supply",
            "acc",
            "feed",
            "Isolation Valve",
            type=ComponentType.BALL_VALVE,
            is_open=True,
        )
    )
    net.add_component(_line(0.732, "acc", "engine", "discharge", "Discharge Line"))

    return Project(
        settings=ProjectSettings(gas_name="O2", t_flow_R=530.0, t_ref_R=530.0),
        network=net,
        name="Accumulator blowdown",
        notes=(
            "Validated reference cases 4 and 5. Run Transient to 1.0 s: with "
            "the isolation valve open you should see ~431.1 psig and ~119 g/s; "
            "close it and the same vessel gives ~331.4 psig and ~96.4 g/s."
        ),
    )


EXAMPLES = {
    "O2 two-branch feed (cases 1-2)": o2_two_branch,
    "CH4 regulator chain (case 3)": ch4_chain,
    "Accumulator blowdown (cases 4-5)": accumulator_blowdown,
}
