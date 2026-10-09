"""Locating the user-editable data files (gas table, component library)."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "PIDSIM_DATA_DIR"


def data_dir() -> Path:
    """Directory holding ``gases.json`` and ``components.json``.

    Override with the ``PIDSIM_DATA_DIR`` environment variable; otherwise this
    resolves to ``pidsim/data``, shipped inside the package itself so it
    travels correctly with any install -- editable, a built wheel, or a
    zipped copy of the repo. (It used to live at the project root as a sibling
    of ``pidsim/``, which only worked for a non-editable install by accident:
    ``package_data`` happened to resolve a ``"../data/*.json"`` pattern that
    escapes the package directory, which isn't a documented, guaranteed
    mechanism. Keeping data inside the package is the standard, supported
    pattern.)
    """
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "data"


def data_file(name: str) -> Path:
    path = data_dir() / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Data file {name!r} not found in {data_dir()}. "
            f"Set {_ENV_VAR} to point at the directory containing it."
        )
    return path
