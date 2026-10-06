"""The five hand-validated reference cases.

These come from validated hand/Python analysis of a real feed system. If the
implementation drifts more than ~1% from these numbers, the bug is in the code,
not in the references. Everything else in the project is built on top of this
file passing.

All cases use the fixed convention set documented in ``pidsim.units``:
14.7 psia / 530 degR SCFH reference, 530 degR flowing temperature.
"""

from __future__ import annotations

import math

import pytest

from pidsim.physics import (
    Regime,
    combine_series_cv,
    mass_flow,
    mass_flow_with_regime,
    p1_for_mass_flow,
    reference_density,
    get_gas,
)
from pidsim.solver.vessel import (
    VesselSpec,
    simulate_coupled,
    simulate_isolated,
)
from pidsim.units import (
    LITER_TO_M3,
    T_REF_R_DEFAULT,
    kelvin_to_rankine,
    psia_to_psig,
    psig_to_psia,
)

TOL = 0.01  # 1%, as specified

T_FLOW = T_REF_R_DEFAULT
T_REF = T_REF_R_DEFAULT

O2 = get_gas("O2")
CH4 = get_gas("CH4")

MANIFOLD_PSIA = 164.7  # 150 psig
BRANCH_MDOT = 0.110  # kg/s per O2 branch


def rel(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected)


def assert_close(actual: float, expected: float, tol: float = TOL, label: str = "") -> None:
    err = rel(actual, expected)
    assert err <= tol, (
        f"{label}: got {actual:.6g}, expected {expected:.6g} "
        f"({err * 100:.3f}% off, tolerance {tol * 100:.1f}%)"
    )


# --- Reference densities (the convention that makes everything else work) ---


def test_reference_densities():
    assert_close(reference_density(O2, T_REF), 1.3248, 1e-3, "rho_ref O2")
    assert_close(reference_density(CH4, T_REF), 0.6641, 1e-3, "rho_ref CH4")


# --- Case 1 ----------------------------------------------------------------


def test_case_1_o2_branch_alone():
    """Cv=0.732, 164.7 psia downstream, 110 g/s -> ~418.2 psia, CHOKED."""
    p1 = p1_for_mass_flow(BRANCH_MDOT, 0.732, MANIFOLD_PSIA, O2, T_FLOW, T_REF)

    assert_close(p1, 418.2, TOL, "case 1 required upstream pressure (psia)")
    assert_close(psia_to_psig(p1), 403.5, TOL, "case 1 required upstream pressure (psig)")

    _, regime = mass_flow_with_regime(0.732, p1, MANIFOLD_PSIA, O2, T_FLOW, T_REF)
    assert regime is Regime.CHOKED

    # Round-trip: the forward direction must return the flow we asked for.
    assert_close(
        mass_flow(0.732, p1, MANIFOLD_PSIA, O2, T_FLOW, T_REF),
        BRANCH_MDOT,
        1e-6,
        "case 1 round-trip mass flow",
    )


# --- Case 2 ----------------------------------------------------------------


def test_case_2_o2_main_chain():
    """Cv=2.394 feeding two case-1 branches (220 g/s) -> ~473.2 psia."""
    branch_inlet = p1_for_mass_flow(BRANCH_MDOT, 0.732, MANIFOLD_PSIA, O2, T_FLOW, T_REF)
    total_mdot = 2 * BRANCH_MDOT

    p_reg = p1_for_mass_flow(total_mdot, 2.394, branch_inlet, O2, T_FLOW, T_REF)

    assert_close(p_reg, 473.2, TOL, "case 2 regulator outlet (psia)")
    assert_close(psia_to_psig(p_reg), 458.5, TOL, "case 2 regulator outlet (psig)")


# --- Case 3 ----------------------------------------------------------------


def test_case_3_ch4_full_chain():
    """Cv=1.883, 150 psig manifold, 60 g/s CH4 -> ~197.2 psia."""
    p_reg = p1_for_mass_flow(0.060, 1.883, MANIFOLD_PSIA, CH4, T_FLOW, T_REF)

    assert_close(p_reg, 197.2, TOL, "case 3 regulator outlet (psia)")
    assert_close(psia_to_psig(p_reg), 182.5, TOL, "case 3 regulator outlet (psig)")

    # Subsonic here, unlike the O2 branch.
    _, regime = mass_flow_with_regime(1.883, p_reg, MANIFOLD_PSIA, CH4, T_FLOW, T_REF)
    assert regime is Regime.SUBSONIC


# --- Cases 4 and 5: transient ---------------------------------------------

ACC_VOLUME = 10.0 * LITER_TO_M3
ACC_CHARGE_PSIA = psig_to_psia(500.0)
CV_OUT = 0.732
CV_IN = 0.923
SOURCE_PSIA = psig_to_psia(500.0)


def _accumulator() -> VesselSpec:
    return VesselSpec(
        volume_m3=ACC_VOLUME,
        charge_pressure_psia=ACC_CHARGE_PSIA,
        charge_temperature_R=T_FLOW,
        gas=O2,
        t_ref_R=T_REF,
        name="accumulator",
    )


def _outflow(t: float, p_psia: float, t_flow_R: float) -> float:
    """Discharge through Cv=0.732 into the 164.7 psia manifold."""
    return mass_flow(CV_OUT, p_psia, MANIFOLD_PSIA, O2, t_flow_R, T_REF)


def _inflow(t: float, p_psia: float) -> tuple[float, float]:
    """Feed from a constant 500 psig source through combined Cv=0.923."""
    mdot = mass_flow(CV_IN, SOURCE_PSIA, p_psia, O2, T_FLOW, T_REF)
    from pidsim.units import rankine_to_kelvin

    return mdot, rankine_to_kelvin(T_FLOW)


def test_case_4_isolated_accumulator():
    """10 L, 500 psig, Cv=0.732 into 164.7 psia -> 331.4 psig, 96.4 g/s at t=1 s."""
    trace = simulate_isolated(_accumulator(), _outflow, (0.0, 1.0))
    at1 = trace.at(1.0)

    assert_close(psia_to_psig(at1["pressure_psia"]), 331.4, TOL, "case 4 P(1 s) psig")
    assert_close(at1["mdot_out_kgs"] * 1e3, 96.4, TOL, "case 4 mdot(1 s) g/s")


def test_case_5_coupled_accumulator():
    """Same vessel, still fed through Cv=0.923 -> 431.1 psig, 119 g/s at t=1 s."""
    trace = simulate_coupled(_accumulator(), _inflow, _outflow, (0.0, 1.0))
    at1 = trace.at(1.0)

    assert_close(psia_to_psig(at1["pressure_psia"]), 431.1, TOL, "case 5 P(1 s) psig")
    assert_close(at1["mdot_out_kgs"] * 1e3, 119.0, TOL, "case 5 mdot(1 s) g/s")


def test_coupled_and_isolated_differ_substantially():
    """The two modes must not be collapsible into one another."""
    iso = simulate_isolated(_accumulator(), _outflow, (0.0, 1.0)).at(1.0)
    cpl = simulate_coupled(_accumulator(), _inflow, _outflow, (0.0, 1.0)).at(1.0)
    assert cpl["pressure_psia"] - iso["pressure_psia"] > 50.0


def test_energy_model_matches_isentropic_when_isolated():
    """Cross-check: for an ideal gas the two formulations agree at zero inflow.

    This validates the mass+energy implementation against case 4's independently
    verified numbers, which is otherwise only reachable through the isentropic
    path.
    """
    no_inflow: tuple[float, float] = (0.0, 0.0)
    trace = simulate_coupled(
        _accumulator(), lambda t, p: no_inflow, _outflow, (0.0, 1.0)
    )
    at1 = trace.at(1.0)

    assert_close(psia_to_psig(at1["pressure_psia"]), 331.4, TOL, "energy-model P(1 s)")
    assert_close(at1["mdot_out_kgs"] * 1e3, 96.4, TOL, "energy-model mdot(1 s)")


# --- Series-Cv consistency with the reference chains -----------------------


def test_series_cv_reciprocal_square_sum():
    """Two identical Cv in series give Cv/sqrt(2)."""
    assert_close(combine_series_cv([1.0, 1.0]), 1.0 / math.sqrt(2.0), 1e-12, "series Cv")


def test_branch_cv_from_reference_components():
    """The 0.732 branch Cv is reproducible from the seeded 3/8" part Cv values.

    The 3/4"M -> 3/8"F reducer (Cv 2.7) in series with five 3/8" elements of
    Cv 1.7 each (NPT-JIC and JIC-hose adapters plus SS braided hose).
    """
    cv = combine_series_cv([2.7] + [1.7] * 5)
    assert_close(cv, 0.732, 0.01, "3/8 in branch Cv")


def test_coupled_feed_cv_from_reference_components():
    """Case 5's combined feed Cv of 0.923 is the same chain minus two elements."""
    cv = combine_series_cv([2.7] + [1.7] * 3)
    assert_close(cv, 0.923, 0.01, "accumulator feed Cv")
