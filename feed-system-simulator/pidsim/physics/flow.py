"""Compressible gas flow through a Cv-rated restriction.

Implements the two-regime Swagelok/Parker-style gas sizing equation, the
standard series-Cv combination rule, and their inverses (solve for the upstream
pressure that delivers a required mass flow).

Unit conventions -- these are load-bearing and were fixed by reproducing the
hand-validated reference cases:

* ``P1``/``P2`` are absolute pressures in **psia**.
* ``T_flow_R`` is the **actual local flowing gas temperature** in degR. It is
  the only temperature that tracks the gas state.
* ``T_ref_R`` is the **fixed** SCFH reference temperature (default 530 degR),
  used together with ``P_REF_PSIA`` = 14.7 psia to turn standard cubic feet
  into kilograms. It must NOT be allowed to follow the flowing temperature --
  doing so silently breaks transient blowdown mass flows.

Regime continuity: at P2 = 0.5*P1 the subsonic branch evaluates to
963*sqrt(3)/2 = 833.982*Cv*P1/sqrt(Sg*T), against the choked branch's
834*Cv*P1/sqrt(Sg*T). The published coefficients are rounded, so the branches
meet with a step of 0.002% rather than exactly -- negligible next to any real
Cv tolerance. The derivative, however, is genuinely discontinuous there (mass
flow goes flat once choked), so root finders and ODE integrators downstream
must not assume smoothness at the choke point.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Iterable

from scipy.optimize import brentq

from ..units import (
    P_REF_PSIA,
    PSI_TO_PA,
    R_UNIVERSAL,
    SECONDS_PER_HOUR,
    T_REF_R_DEFAULT,
    FT3_TO_M3,
    rankine_to_kelvin,
)
from .gases import Gas

# Coefficients of the two-regime gas sizing equation.
C_SUBSONIC = 963.0
C_CHOKED = 834.0

CHOKE_RATIO = 0.5
"""Flow is choked once P2 <= 0.5 * P1."""

_EPS = 1e-12


class Regime(str, Enum):
    SUBSONIC = "subsonic"
    CHOKED = "choked"
    NO_FLOW = "no_flow"


def regime_for(p1_psia: float, p2_psia: float) -> Regime:
    if p1_psia <= p2_psia + _EPS:
        return Regime.NO_FLOW
    return Regime.CHOKED if p2_psia <= CHOKE_RATIO * p1_psia else Regime.SUBSONIC


def scfh_from_cv(
    cv: float,
    p1_psia: float,
    p2_psia: float,
    sg: float,
    t_flow_R: float,
) -> tuple[float, Regime]:
    """Volumetric flow in SCFH through a restriction of flow coefficient ``cv``.

    Returns ``(Q_scfh, regime)``.
    """
    if cv <= 0.0:
        return 0.0, Regime.NO_FLOW

    regime = regime_for(p1_psia, p2_psia)
    if regime is Regime.NO_FLOW:
        return 0.0, regime

    if regime is Regime.CHOKED:
        q = C_CHOKED * cv * p1_psia * math.sqrt(1.0 / (sg * t_flow_R))
    else:
        q = C_SUBSONIC * cv * math.sqrt((p1_psia**2 - p2_psia**2) / (sg * t_flow_R))
    return q, regime


def reference_density(gas: Gas, t_ref_R: float = T_REF_R_DEFAULT) -> float:
    """Density at the FIXED SCFH reference state (14.7 psia, ``t_ref_R``), kg/m^3.

    This is what one "standard cubic foot" weighs. It is a property of the
    reference state only -- never of the current flowing conditions.
    """
    p_pa = P_REF_PSIA * PSI_TO_PA
    return p_pa * gas.molar_mass / (R_UNIVERSAL * rankine_to_kelvin(t_ref_R))


def scfh_to_mass_flow(
    q_scfh: float, gas: Gas, t_ref_R: float = T_REF_R_DEFAULT
) -> float:
    """SCFH -> kg/s using the fixed reference density."""
    return q_scfh * FT3_TO_M3 / SECONDS_PER_HOUR * reference_density(gas, t_ref_R)


def mass_flow(
    cv: float,
    p1_psia: float,
    p2_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
) -> float:
    """Mass flow (kg/s) from ``p1`` to ``p2``. Zero if ``p2 >= p1``."""
    q, _ = scfh_from_cv(cv, p1_psia, p2_psia, gas.Sg, t_flow_R)
    return scfh_to_mass_flow(q, gas, t_ref_R)


def mass_flow_with_regime(
    cv: float,
    p1_psia: float,
    p2_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
) -> tuple[float, Regime]:
    q, regime = scfh_from_cv(cv, p1_psia, p2_psia, gas.Sg, t_flow_R)
    return scfh_to_mass_flow(q, gas, t_ref_R), regime


def signed_mass_flow(
    cv: float,
    pa_psia: float,
    pb_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
    allow_reverse: bool = True,
) -> float:
    """Mass flow from node A to node B, negative when it actually runs B -> A.

    Antisymmetric in (pa, pb), which is what makes the nodal solver's residuals
    well posed. ``allow_reverse=False`` models a check valve.

    Note: a single ``t_flow_R`` is used in both directions. Reverse flow through
    a component whose two sides sit at very different temperatures is therefore
    approximate; in practice reverse flow only appears transiently during a
    solve, or is blocked outright by a check valve.
    """
    if pa_psia >= pb_psia:
        return mass_flow(cv, pa_psia, pb_psia, gas, t_flow_R, t_ref_R)
    if not allow_reverse:
        return 0.0
    return -mass_flow(cv, pb_psia, pa_psia, gas, t_flow_R, t_ref_R)


def choked_mass_flow(
    cv: float,
    p1_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
) -> float:
    """The maximum mass flow ``cv`` can pass at upstream pressure ``p1``."""
    q = C_CHOKED * cv * p1_psia * math.sqrt(1.0 / (gas.Sg * t_flow_R))
    return scfh_to_mass_flow(q, gas, t_ref_R)


# --- Inverses --------------------------------------------------------------


def p1_for_mass_flow(
    mdot_kgs: float,
    cv: float,
    p2_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
    p1_max_psia: float = 1.0e6,
) -> float:
    """Upstream pressure required to pass ``mdot`` into backpressure ``p2``.

    This is the backward/sizing direction that produces the reference cases.
    Mass flow is strictly increasing in ``p1``, so a bracketed solve is robust.
    """
    if mdot_kgs <= 0.0:
        return p2_psia
    if cv <= 0.0:
        raise ValueError("Cannot pass flow through a component with Cv <= 0")

    def residual(p1: float) -> float:
        return mass_flow(cv, p1, p2_psia, gas, t_flow_R, t_ref_R) - mdot_kgs

    lo = max(p2_psia, _EPS)
    hi = max(lo * 2.0, lo + 1.0)
    while residual(hi) < 0.0:
        hi *= 2.0
        if hi > p1_max_psia:
            raise ValueError(
                f"Cv={cv:g} cannot pass {mdot_kgs * 1e3:.3f} g/s into "
                f"{p2_psia:.1f} psia below {p1_max_psia:g} psia"
            )
    return brentq(residual, lo, hi, xtol=1e-10, rtol=1e-14, maxiter=200)


def p2_for_mass_flow(
    mdot_kgs: float,
    cv: float,
    p1_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
) -> float:
    """Backpressure that results when ``mdot`` is drawn through ``cv`` from ``p1``.

    Mass flow is flat with respect to ``p2`` once choked, so if ``mdot`` equals
    the choked value the answer is not unique; the choke point ``0.5*p1`` is
    returned as the highest backpressure consistent with that flow.
    """
    if mdot_kgs <= 0.0:
        return p1_psia
    m_choked = choked_mass_flow(cv, p1_psia, gas, t_flow_R, t_ref_R)
    if mdot_kgs > m_choked * (1.0 + 1e-9):
        raise ValueError(
            f"Cv={cv:g} at {p1_psia:.1f} psia is choked at "
            f"{m_choked * 1e3:.3f} g/s; {mdot_kgs * 1e3:.3f} g/s is unreachable"
        )
    if mdot_kgs >= m_choked * (1.0 - 1e-9):
        return CHOKE_RATIO * p1_psia

    def residual(p2: float) -> float:
        return mass_flow(cv, p1_psia, p2, gas, t_flow_R, t_ref_R) - mdot_kgs

    return brentq(
        residual, CHOKE_RATIO * p1_psia, p1_psia, xtol=1e-10, rtol=1e-14, maxiter=200
    )


def cv_for_mass_flow(
    mdot_kgs: float,
    p1_psia: float,
    p2_psia: float,
    gas: Gas,
    t_flow_R: float,
    t_ref_R: float = T_REF_R_DEFAULT,
) -> float:
    """Flow coefficient needed to pass ``mdot`` across a given pressure drop.

    Mass flow is exactly linear in Cv in both regimes, so this is a direct
    scaling rather than a root find.
    """
    if p1_psia <= p2_psia:
        raise ValueError("Requires p1 > p2")
    unit = mass_flow(1.0, p1_psia, p2_psia, gas, t_flow_R, t_ref_R)
    return mdot_kgs / unit


# --- Combination rules -----------------------------------------------------


def combine_series_cv(cvs: Iterable[float]) -> float:
    """Equivalent Cv of components in series: 1/Cv_tot^2 = sum(1/Cv_i^2).

    This reciprocal-square-sum is the standard engineering approximation, not
    an exact result: it assumes incompressible-style additive resistances and
    ignores pressure recovery between elements and any change of regime along
    the chain. It is what the hand-validated reference chains used, so the
    simulator reproduces it exactly.
    """
    total = 0.0
    for cv in cvs:
        if cv <= 0.0:
            return 0.0  # a shut component blocks the whole series path
        total += 1.0 / (cv * cv)
    if total == 0.0:
        raise ValueError("combine_series_cv() requires at least one component")
    return 1.0 / math.sqrt(total)


def combine_parallel_cv(cvs: Iterable[float]) -> float:
    """Equivalent Cv of components in parallel: Cv_tot = sum(Cv_i).

    Provided for completeness. The solver does NOT use this for branches -- it
    solves the junction pressure implicitly instead, because parallel paths
    with different downstream boundary conditions do not share a pressure drop.
    """
    return float(sum(cvs))
