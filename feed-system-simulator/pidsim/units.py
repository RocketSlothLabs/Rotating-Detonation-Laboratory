"""Unit conversions and physical constants.

Single source of truth for every magic number in the simulator. The Cv sizing
equations in :mod:`pidsim.physics.flow` are written in US customary units
(psia, degR, SCFH); everything internal to the solvers is SI (Pa, K, kg, m^3).

The SCFH reference state is deliberately FIXED (see ``P_REF_PSIA`` /
``T_REF_R_DEFAULT``). Only the temperature *inside* the valve equation tracks
the actual local flowing gas; the standard-cubic-foot reference density must
not float with it, or transient blowdown mass flows come out wrong.
"""

from __future__ import annotations

# --- Fundamental constants -------------------------------------------------

R_UNIVERSAL = 8.314462618
"""Universal gas constant, J/(mol*K)."""

M_AIR = 28.9647e-3
"""Molar mass of air, kg/mol. Reference for specific gravity Sg = M / M_AIR."""

# --- Conversion factors ----------------------------------------------------

PSI_TO_PA = 6894.757
"""Pounds per square inch to pascals."""

FT3_TO_M3 = 0.0283168
"""Cubic feet to cubic metres."""

SECONDS_PER_HOUR = 3600.0

LITER_TO_M3 = 1.0e-3

# --- Reference / ambient state --------------------------------------------

P_ATM_PSIA = 14.6959
"""Standard atmospheric pressure, psia. Used for psig <-> psia conversion."""

P_REF_PSIA = 14.7
"""SCFH reference pressure, psia. Fixed; do not tie to ambient."""

T_REF_R_DEFAULT = 530.0
"""SCFH reference temperature, degR (70 degF).

This value is not arbitrary: it is the only reference temperature that
reproduces the hand-validated reference cases, giving reference densities of
1.3248 kg/m^3 for O2 and 0.6641 kg/m^3 for CH4.
"""

T_FLOW_R_DEFAULT = 530.0
"""Default flowing gas temperature, degR. Overridable per project."""


# --- Pressure --------------------------------------------------------------


def psig_to_psia(p_psig: float) -> float:
    return p_psig + P_ATM_PSIA


def psia_to_psig(p_psia: float) -> float:
    return p_psia - P_ATM_PSIA


def psi_to_pa(p_psi: float) -> float:
    return p_psi * PSI_TO_PA


def pa_to_psi(p_pa: float) -> float:
    return p_pa / PSI_TO_PA


# --- Temperature -----------------------------------------------------------


def rankine_to_kelvin(t_r: float) -> float:
    return t_r * 5.0 / 9.0


def kelvin_to_rankine(t_k: float) -> float:
    return t_k * 9.0 / 5.0


def fahrenheit_to_rankine(t_f: float) -> float:
    return t_f + 459.67


# --- Volumetric / mass flow ------------------------------------------------


def scfh_to_m3s(q_scfh: float) -> float:
    """Standard cubic feet per hour to cubic metres per second (at ref state)."""
    return q_scfh * FT3_TO_M3 / SECONDS_PER_HOUR
