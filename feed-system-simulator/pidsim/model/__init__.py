"""Data model. Depends on ``physics`` only -- never on ``solver`` or ``gui``."""

from .components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
    SourceMode,
    ValveSchedule,
    VesselModel,
    series_component,
)
from .library import LibraryPart, get_part, part_library, parts_of_type
from .network import Network, NetworkError, Severity, ValidationIssue
from .project import Project, ProjectSettings, from_dict, load, save, to_dict

__all__ = [
    "ComponentType",
    "FlowComponent",
    "LibraryPart",
    "Network",
    "NetworkError",
    "Node",
    "NodeKind",
    "Project",
    "ProjectSettings",
    "Severity",
    "SinkSpec",
    "SourceMode",
    "ValidationIssue",
    "ValveSchedule",
    "VesselModel",
    "from_dict",
    "get_part",
    "load",
    "part_library",
    "parts_of_type",
    "save",
    "series_component",
    "to_dict",
]
