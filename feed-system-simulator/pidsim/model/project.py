"""Project-level settings and JSON save/load.

The saved file is the single source of truth for a design: settings, topology,
component specs and canvas placement, all in one versioned document.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from ..physics.gases import Gas, get_gas
from ..units import (
    P_REF_PSIA,
    T_FLOW_R_DEFAULT,
    T_REF_R_DEFAULT,
)
from .components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
    SourceMode,
    ValveSchedule,
    VesselModel,
)
from .network import Network

FILE_VERSION = 1


@dataclass
class ProjectSettings:
    """Everything that applies to the whole design rather than one component."""

    gas_name: str = "O2"

    t_flow_R: float = T_FLOW_R_DEFAULT
    """Flowing gas temperature, degR. Used inside the Cv equation."""

    t_ref_R: float = T_REF_R_DEFAULT
    """SCFH reference temperature, degR. Fixed; see pidsim.units."""

    p_ref_psia: float = P_REF_PSIA
    """SCFH reference pressure, psia."""

    ambient_pressure_psia: float = 14.6959

    @property
    def gas(self) -> Gas:
        return get_gas(self.gas_name)


@dataclass
class Project:
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    network: Network = field(default_factory=Network)
    name: str = "Untitled"
    notes: str = ""


# --- serialisation ---------------------------------------------------------


def _clean(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _from_dict(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, ignoring unknown keys and coercing enums."""
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            continue  # forward compatibility: ignore fields we don't know
        kwargs[key] = value
    return cls(**kwargs)


def to_dict(project: Project) -> dict[str, Any]:
    return {
        "version": FILE_VERSION,
        "name": project.name,
        "notes": project.notes,
        "settings": _clean(asdict(project.settings)),
        "network": {
            "name": project.network.name,
            "nodes": [_clean(asdict(n)) for n in project.network.nodes.values()],
            "components": [
                _clean(asdict(c)) for c in project.network.components.values()
            ],
        },
    }


def _node_from_dict(data: dict[str, Any]) -> Node:
    data = dict(data)
    data["kind"] = NodeKind(data.get("kind", "junction"))
    data["source_mode"] = SourceMode(data.get("source_mode", "ideal_constant"))
    data["sink_spec"] = SinkSpec(data.get("sink_spec", "both"))
    data["vessel_model"] = VesselModel(data.get("vessel_model", "auto"))
    return _from_dict(Node, data)


def _component_from_dict(data: dict[str, Any]) -> FlowComponent:
    data = dict(data)
    data["type"] = ComponentType(data["type"])
    sched = data.get("schedule")
    data["schedule"] = _from_dict(ValveSchedule, sched) if sched else None
    return _from_dict(FlowComponent, data)


def from_dict(data: dict[str, Any]) -> Project:
    version = data.get("version", FILE_VERSION)
    if version > FILE_VERSION:
        raise ValueError(
            f"Design file version {version} is newer than this build supports "
            f"({FILE_VERSION}). Update the app."
        )

    settings = _from_dict(ProjectSettings, data.get("settings", {}))
    net_data = data.get("network", {})
    network = Network(name=net_data.get("name", "Untitled"))
    for node_data in net_data.get("nodes", []):
        network.add_node(_node_from_dict(node_data))
    for comp_data in net_data.get("components", []):
        network.add_component(_component_from_dict(comp_data))

    return Project(
        settings=settings,
        network=network,
        name=data.get("name", "Untitled"),
        notes=data.get("notes", ""),
    )


def save(project: Project, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(to_dict(project), indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> Project:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return from_dict(data)
