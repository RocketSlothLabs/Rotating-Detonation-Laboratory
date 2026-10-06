"""Dynamic vessel state: accumulators and blowing-down tanks.

This module owns the two formulations the project requires and deliberately
keeps them separate, because they give substantially different answers when an
upstream supply is present:

**Isolated** -- the vessel has been cut off (a valve upstream is shut). The
expansion is adiabatic and isentropic, one state variable (pressure) suffices::

    dP/dt = -mdot_out * gamma * P^(1 - 1/gamma) / K
    T(t)  = T1 * (P/P1)^((gamma-1)/gamma)

**Coupled** -- the vessel is still being fed. Warm gas entering at the source
temperature mixes with the cold gas already inside, so entropy is not
conserved and two state variables are needed::

    dm/dt = mdot_in - mdot_out
    dU/dt = mdot_in*cp*T_in - mdot_out*cp*T_local
    T     = U/(m*cv),  P = m*R*T/V

The asymmetry in ``dU/dt`` is the point: **inflow enthalpy is evaluated at the
upstream source temperature, outflow enthalpy at the current local vessel
temperature**. Evaluating both at the same temperature is the classic way to
get this wrong.

The functions here provide the derivatives; :mod:`pidsim.solver.transient`
assembles them into a whole-network state vector, and the ``simulate_*``
helpers integrate a single vessel directly (which is what the reference cases
exercise).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

import numpy as np
from scipy.integrate import solve_ivp

from ..physics import isentropic as isen
from ..physics.gases import Gas
from ..units import (
    PSI_TO_PA,
    T_REF_R_DEFAULT,
    kelvin_to_rankine,
    pa_to_psi,
    psi_to_pa,
    rankine_to_kelvin,
)


class VesselModel(str, Enum):
    """Which formulation to integrate for a given vessel."""

    ISENTROPIC = "isentropic"
    """Isolated: 1 state (pressure). Assumes no inflow for the whole run."""

    ENERGY = "energy"
    """Coupled: 2 states (mass, internal energy). Handles inflow correctly."""

    AUTO = "auto"
    """Pick ISENTROPIC if no upstream path can ever open, else ENERGY."""


class OutflowFn(Protocol):
    def __call__(self, t: float, p_psia: float, t_flow_R: float) -> float:
        """Mass flow leaving the vessel, kg/s."""


class InflowFn(Protocol):
    def __call__(self, t: float, p_psia: float) -> tuple[float, float]:
        """``(mdot_in kg/s, T_in in K)`` entering the vessel."""


@dataclass(frozen=True)
class VesselSpec:
    """Everything the dynamics need about one vessel."""

    volume_m3: float
    charge_pressure_psia: float
    charge_temperature_R: float
    gas: Gas
    t_ref_R: float = T_REF_R_DEFAULT
    name: str = "vessel"

    # --- derived -----------------------------------------------------------

    @property
    def gamma(self) -> float:
        return self.gas.gamma

    @property
    def R(self) -> float:
        return self.gas.R_specific

    @property
    def charge_pressure_pa(self) -> float:
        return psi_to_pa(self.charge_pressure_psia)

    @property
    def charge_temperature_K(self) -> float:
        return rankine_to_kelvin(self.charge_temperature_R)

    @property
    def K(self) -> float:
        """Blowdown grouping ``m = K * P^(1/gamma)`` (SI units throughout)."""
        return isen.blowdown_K(
            self.volume_m3,
            self.charge_pressure_pa,
            self.R,
            self.charge_temperature_K,
            self.gamma,
        )

    @property
    def initial_mass(self) -> float:
        return isen.ideal_gas_mass(
            self.charge_pressure_pa, self.volume_m3, self.R, self.charge_temperature_K
        )

    @property
    def initial_energy(self) -> float:
        return self.initial_mass * self.gas.cv * self.charge_temperature_K


@dataclass
class VesselTrace:
    """Time history of one vessel."""

    t: np.ndarray
    pressure_psia: np.ndarray
    temperature_K: np.ndarray
    mass_kg: np.ndarray
    mdot_out_kgs: np.ndarray
    mdot_in_kgs: np.ndarray
    model: VesselModel
    name: str = "vessel"
    events: list[str] = field(default_factory=list)

    def at(self, t_query: float) -> dict[str, float]:
        """Linear interpolation of the trace at a single time."""
        return {
            "t": t_query,
            "pressure_psia": float(np.interp(t_query, self.t, self.pressure_psia)),
            "temperature_K": float(np.interp(t_query, self.t, self.temperature_K)),
            "mass_kg": float(np.interp(t_query, self.t, self.mass_kg)),
            "mdot_out_kgs": float(np.interp(t_query, self.t, self.mdot_out_kgs)),
            "mdot_in_kgs": float(np.interp(t_query, self.t, self.mdot_in_kgs)),
        }


# --- Isolated (isentropic) -------------------------------------------------


def isolated_temperature_K(spec: VesselSpec, p_psia: float) -> float:
    """Local gas temperature after isentropic expansion to ``p_psia``."""
    return isen.temperature_at(
        psi_to_pa(p_psia),
        spec.charge_pressure_pa,
        spec.charge_temperature_K,
        spec.gamma,
    )


def isolated_dPdt_psia(spec: VesselSpec, p_psia: float, mdot_out: float) -> float:
    """dP/dt in psia/s for the isolated case."""
    dp_pa = isen.dPdt(mdot_out, psi_to_pa(p_psia), spec.K, spec.gamma)
    return pa_to_psi(dp_pa)


# --- Coupled (mass + energy) ----------------------------------------------


def coupled_state(spec: VesselSpec, m_kg: float, u_j: float) -> tuple[float, float]:
    """``(T in K, P in psia)`` from the two state variables."""
    t_k = u_j / (m_kg * spec.gas.cv)
    p_pa = isen.ideal_gas_pressure(m_kg, spec.volume_m3, spec.R, t_k)
    return t_k, pa_to_psi(p_pa)


def coupled_derivatives(
    spec: VesselSpec,
    t_local_K: float,
    mdot_in: float,
    t_in_K: float,
    mdot_out: float,
) -> tuple[float, float]:
    """``(dm/dt, dU/dt)``.

    Inflow enthalpy uses the upstream ``t_in_K``; outflow enthalpy uses the
    vessel's own ``t_local_K``.
    """
    cp = spec.gas.cp
    dm = mdot_in - mdot_out
    du = mdot_in * cp * t_in_K - mdot_out * cp * t_local_K
    return dm, du


# --- Single-vessel integration helpers -------------------------------------

_DEFAULT_RTOL = 1e-8
_DEFAULT_ATOL = 1e-10


def simulate_isolated(
    spec: VesselSpec,
    outflow: OutflowFn,
    t_span: tuple[float, float],
    n_output: int = 501,
    p_floor_psia: float = 0.0,
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
) -> VesselTrace:
    """Integrate an isolated vessel blowing down through ``outflow``.

    ``outflow(t, p_psia, t_flow_R)`` returns kg/s; the vessel supplies its own
    isentropically-cooled temperature as ``t_flow_R``.
    """

    def rhs(t: float, y: np.ndarray) -> list[float]:
        p = max(y[0], 1e-9)
        t_local = isolated_temperature_K(spec, p)
        mdot = outflow(t, p, kelvin_to_rankine(t_local))
        return [isolated_dPdt_psia(spec, p, mdot)]

    events = []
    if p_floor_psia > 0.0:

        def depleted(t: float, y: np.ndarray) -> float:
            return y[0] - p_floor_psia

        depleted.terminal = True
        depleted.direction = -1.0
        events.append(depleted)

    sol = solve_ivp(
        rhs,
        t_span,
        [spec.charge_pressure_psia],
        method="LSODA",
        rtol=rtol,
        atol=atol,
        dense_output=True,
        events=events or None,
    )
    if not sol.success:
        raise RuntimeError(f"Isolated blowdown failed to integrate: {sol.message}")

    t_end = sol.t[-1]
    t_out = np.linspace(t_span[0], t_end, n_output)
    p_out = np.clip(sol.sol(t_out)[0], 1e-9, None)
    temp = np.array([isolated_temperature_K(spec, p) for p in p_out])
    mdot = np.array(
        [
            outflow(ti, pi, kelvin_to_rankine(Ti))
            for ti, pi, Ti in zip(t_out, p_out, temp)
        ]
    )
    mass = np.array(
        [isen.mass_from_pressure(psi_to_pa(p), spec.K, spec.gamma) for p in p_out]
    )
    notes = []
    if sol.status == 1:
        notes.append(f"{spec.name} reached {p_floor_psia:.2f} psia at t={t_end:.4f} s")
    return VesselTrace(
        t=t_out,
        pressure_psia=p_out,
        temperature_K=temp,
        mass_kg=mass,
        mdot_out_kgs=mdot,
        mdot_in_kgs=np.zeros_like(mdot),
        model=VesselModel.ISENTROPIC,
        name=spec.name,
        events=notes,
    )


def simulate_coupled(
    spec: VesselSpec,
    inflow: InflowFn,
    outflow: OutflowFn,
    t_span: tuple[float, float],
    n_output: int = 501,
    p_floor_psia: float = 0.0,
    rtol: float = _DEFAULT_RTOL,
    atol: float = _DEFAULT_ATOL,
) -> VesselTrace:
    """Integrate a vessel that is still fed from upstream.

    ``inflow(t, p_psia) -> (mdot_kgs, T_in_K)``; a shut upstream valve simply
    returns ``(0.0, anything)``, which is how a mid-run isolation is modeled
    without switching formulations.
    """

    def unpack(y: np.ndarray) -> tuple[float, float, float, float]:
        m = max(y[0], 1e-12)
        u = y[1]
        t_k, p_psia = coupled_state(spec, m, u)
        return m, u, t_k, p_psia

    def rhs(t: float, y: np.ndarray) -> list[float]:
        _, _, t_k, p_psia = unpack(y)
        mdot_in, t_in = inflow(t, p_psia)
        mdot_out = outflow(t, p_psia, kelvin_to_rankine(t_k))
        return list(coupled_derivatives(spec, t_k, mdot_in, t_in, mdot_out))

    events = []
    if p_floor_psia > 0.0:

        def depleted(t: float, y: np.ndarray) -> float:
            return unpack(y)[3] - p_floor_psia

        depleted.terminal = True
        depleted.direction = -1.0
        events.append(depleted)

    y0 = [spec.initial_mass, spec.initial_energy]
    sol = solve_ivp(
        rhs,
        t_span,
        y0,
        method="LSODA",
        rtol=rtol,
        atol=atol,
        dense_output=True,
        events=events or None,
    )
    if not sol.success:
        raise RuntimeError(f"Coupled vessel failed to integrate: {sol.message}")

    t_end = sol.t[-1]
    t_out = np.linspace(t_span[0], t_end, n_output)
    ys = sol.sol(t_out)
    mass = np.clip(ys[0], 1e-12, None)
    temp = np.empty_like(mass)
    press = np.empty_like(mass)
    mdot_in = np.empty_like(mass)
    mdot_out = np.empty_like(mass)
    for i, (ti, mi, ui) in enumerate(zip(t_out, mass, ys[1])):
        t_k, p_psia = coupled_state(spec, mi, ui)
        temp[i] = t_k
        press[i] = p_psia
        mdot_in[i] = inflow(ti, p_psia)[0]
        mdot_out[i] = outflow(ti, p_psia, kelvin_to_rankine(t_k))

    notes = []
    if sol.status == 1:
        notes.append(f"{spec.name} reached {p_floor_psia:.2f} psia at t={t_end:.4f} s")
    return VesselTrace(
        t=t_out,
        pressure_psia=press,
        temperature_K=temp,
        mass_kg=mass,
        mdot_out_kgs=mdot_out,
        mdot_in_kgs=mdot_in,
        model=VesselModel.ENERGY,
        name=spec.name,
        events=notes,
    )
