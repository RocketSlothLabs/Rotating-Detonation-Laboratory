"""Isentropic (adiabatic) relations for vessel blowdown.

Two formulations live here, and the distinction matters:

* **Isolated** -- a vessel cut off from any upstream supply. Expansion is
  isentropic, so pressure alone is enough state, and ``dP/dt`` closes in terms
  of the outflow.
* **Coupled** -- a vessel still being fed. Incoming gas at the source
  temperature mixes with colder gas already in the vessel, so the process is
  no longer isentropic and mass and internal energy must both be tracked.
  That lives in :mod:`pidsim.solver.transient`; the helpers here are the
  isolated case plus the state relations both share.

For an ideal gas the two are mathematically equivalent when the inflow is zero
(``dU/dt = -mdot*cp*T`` integrates to the isentropic result), which the test
suite asserts as a cross-check.

Pressures here are in **pascals**, temperatures in **kelvin**, volumes in m^3.
Mixing psia into these formulas gives a wrong ``K`` and a wrong ``dP/dt``.
"""

from __future__ import annotations


def temperature_ratio(p2_pa: float, p1_pa: float, gamma: float) -> float:
    """T2/T1 = (P2/P1)^((gamma-1)/gamma)."""
    return (p2_pa / p1_pa) ** ((gamma - 1.0) / gamma)


def density_ratio(p2_pa: float, p1_pa: float, gamma: float) -> float:
    """rho2/rho1 = (P2/P1)^(1/gamma)."""
    return (p2_pa / p1_pa) ** (1.0 / gamma)


def temperature_at(p_pa: float, p1_pa: float, t1_k: float, gamma: float) -> float:
    """Local gas temperature after isentropic expansion from (p1, t1) to p."""
    return t1_k * temperature_ratio(p_pa, p1_pa, gamma)


def blowdown_K(
    volume_m3: float, p_charge_pa: float, r_specific: float, t1_k: float, gamma: float
) -> float:
    """K = V * P_charge^((gamma-1)/gamma) / (R*T1).

    The grouping that makes vessel mass a pure function of pressure:
    ``m(P) = K * P^(1/gamma)``.
    """
    return volume_m3 * p_charge_pa ** ((gamma - 1.0) / gamma) / (r_specific * t1_k)


def mass_from_pressure(p_pa: float, k: float, gamma: float) -> float:
    """m(t) = K * P(t)^(1/gamma)."""
    return k * p_pa ** (1.0 / gamma)


def pressure_from_mass(m_kg: float, k: float, gamma: float) -> float:
    """Inverse of :func:`mass_from_pressure`."""
    return (m_kg / k) ** gamma


def dPdt(mdot_out_kgs: float, p_pa: float, k: float, gamma: float) -> float:
    """dP/dt = -mdot_out * gamma * P^(1 - 1/gamma) / K, in Pa/s.

    Differentiate ``m = K*P^(1/gamma)`` and substitute ``dm/dt = -mdot_out``.
    """
    return -mdot_out_kgs * gamma * p_pa ** (1.0 - 1.0 / gamma) / k


def ideal_gas_mass(p_pa: float, volume_m3: float, r_specific: float, t_k: float) -> float:
    """m = P*V/(R*T)."""
    return p_pa * volume_m3 / (r_specific * t_k)


def ideal_gas_pressure(m_kg: float, volume_m3: float, r_specific: float, t_k: float) -> float:
    """P = m*R*T/V."""
    return m_kg * r_specific * t_k / volume_m3
