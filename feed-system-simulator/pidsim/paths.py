"""Locating the user-editable data files (gas table, component library)."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "PIDSIM_DATA_DIR"


def data_dir() -> Path:
    """Directory holding ``gases.json`` and ``components.json``.

    Override with the ``PIDSIM_DATA_DIR`` environment variable; otherwise this
    resolves to ``<project root>/data`` next to the ``pidsim`` package.
    """
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data"


def data_file(name: str) -> Path:
    path = data_dir() / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Data file {name!r} not found in {data_dir()}. "
            f"Set {_ENV_VAR} to point at the directory containing it."
        )
    return path
