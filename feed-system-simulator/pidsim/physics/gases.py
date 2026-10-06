"""Ideal-gas property data.

The table is loaded from ``data/gases.json`` so it can be extended without
touching code. Everything downstream treats gases as ideal and calorically
perfect (constant gamma), which is the stated scope for v1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from ..paths import data_file
from ..units import M_AIR, R_UNIVERSAL


@dataclass(frozen=True)
class Gas:
    """An ideal, calorically perfect gas."""

    name: str
    molar_mass: float
    """kg/mol."""
    gamma: float
    viscosity: float | None = None
    """Dynamic viscosity, Pa*s. Informational for v1; not used by the Cv model."""
    display_name: str = ""

    @property
    def R_specific(self) -> float:
        """Specific gas constant, J/(kg*K)."""
        return R_UNIVERSAL / self.molar_mass

    @property
    def cv(self) -> float:
        """Specific heat at constant volume, J/(kg*K)."""
        return self.R_specific / (self.gamma - 1.0)

    @property
    def cp(self) -> float:
        """Specific heat at constant pressure, J/(kg*K)."""
        return self.gamma * self.cv

    @property
    def Sg(self) -> float:
        """Specific gravity relative to air, as used by the Cv sizing equation."""
        return self.molar_mass / M_AIR


def _parse(entry: dict) -> Gas:
    return Gas(
        name=entry["name"],
        # Table is authored in g/mol for readability; store kg/mol internally.
        molar_mass=entry["molar_mass"] * 1e-3,
        gamma=entry["gamma"],
        viscosity=entry.get("viscosity"),
        display_name=entry.get("display_name", entry["name"]),
    )


@lru_cache(maxsize=1)
def gas_table() -> dict[str, Gas]:
    """All gases from ``data/gases.json``, keyed by name (case-insensitive)."""
    with open(data_file("gases.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    return {entry["name"].lower(): _parse(entry) for entry in raw["gases"]}


def get_gas(name: str) -> Gas:
    table = gas_table()
    try:
        return table[name.lower()]
    except KeyError:
        raise KeyError(
            f"Unknown gas {name!r}. Available: {sorted(g.name for g in table.values())}"
        ) from None


def available_gases() -> list[str]:
    return [g.name for g in gas_table().values()]
