"""The user-editable part library (``data/components.json``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache

from ..paths import data_file
from .components import ComponentType, FlowComponent


@dataclass(frozen=True)
class LibraryPart:
    """One catalogue entry. Instantiated onto the canvas as a FlowComponent."""

    key: str
    name: str
    type: ComponentType
    cv: float
    manufacturer: str = ""
    part_number: str = ""
    inlet_size: str = ""
    outlet_size: str = ""
    connection_type: str = ""
    max_pressure_psig: float | None = None
    outlet_setpoint_psig: float | None = None
    inlet_min_psig: float | None = None
    inlet_max_psig: float | None = None
    outlet_min_psig: float | None = None
    outlet_max_psig: float | None = None
    notes: str = ""

    def instantiate(self, from_node: str, to_node: str) -> FlowComponent:
        return FlowComponent(
            type=self.type,
            from_node=from_node,
            to_node=to_node,
            name=self.name,
            manufacturer=self.manufacturer,
            part_number=self.part_number,
            library_key=self.key,
            cv=self.cv,
            inlet_size=self.inlet_size,
            outlet_size=self.outlet_size,
            connection_type=self.connection_type,
            max_pressure_psig=self.max_pressure_psig,
            outlet_setpoint_psig=self.outlet_setpoint_psig,
            inlet_min_psig=self.inlet_min_psig,
            inlet_max_psig=self.inlet_max_psig,
            outlet_min_psig=self.outlet_min_psig,
            outlet_max_psig=self.outlet_max_psig,
        )


def _parse(entry: dict) -> LibraryPart:
    return LibraryPart(
        key=entry["key"],
        name=entry["name"],
        type=ComponentType(entry["type"]),
        cv=float(entry["cv"]),
        manufacturer=entry.get("manufacturer", ""),
        part_number=entry.get("part_number", ""),
        inlet_size=entry.get("inlet_size", ""),
        outlet_size=entry.get("outlet_size", ""),
        connection_type=entry.get("connection_type", ""),
        max_pressure_psig=entry.get("max_pressure_psig"),
        outlet_setpoint_psig=entry.get("outlet_setpoint_psig"),
        inlet_min_psig=entry.get("inlet_min_psig"),
        inlet_max_psig=entry.get("inlet_max_psig"),
        outlet_min_psig=entry.get("outlet_min_psig"),
        outlet_max_psig=entry.get("outlet_max_psig"),
        notes=entry.get("notes", ""),
    )


@lru_cache(maxsize=1)
def part_library() -> dict[str, LibraryPart]:
    """All catalogue parts, keyed by ``key``."""
    with open(data_file("components.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    return {entry["key"]: _parse(entry) for entry in raw["parts"]}


def get_part(key: str) -> LibraryPart:
    try:
        return part_library()[key]
    except KeyError:
        raise KeyError(
            f"Unknown part {key!r}. Available: {sorted(part_library())}"
        ) from None


def parts_of_type(type_: ComponentType) -> list[LibraryPart]:
    return [p for p in part_library().values() if p.type is type_]
