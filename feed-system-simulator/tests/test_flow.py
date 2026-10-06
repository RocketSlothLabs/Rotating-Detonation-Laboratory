"""Unit tests for the Cv flow model itself (properties, not reference numbers)."""

from __future__ import annotations

import math

import pytest

from pidsim.physics import flow
from pidsim.physics.gases import get_gas
from pidsim.units import T_REF_R_DEFAULT

O2 = get_gas("O2")
CH4 = get_gas("CH4")
T = T_REF_R_DEFAULT


def test_regime_coefficients_nearly_meet_at_the_choke_point():
    """963*sqrt(3)/2 = 833.982 vs the published 834: a 0.002% step, not exact."""
    subsonic_at_choke = flow.C_SUBSONIC * math.sqrt(3.0) / 2.0
    assert subsonic_at_choke == pytest.approx(833.9825, rel=1e-6)
    step = abs(subsonic_at_choke - flow.C_CHOKED) / flow.C_CHOKED
    assert step < 5e-5


def test_mass_flow_is_continuous_across_the_regime_switch():
    """Continuous to within the coefficient rounding, with no larger jump."""
    p1 = 400.0
    p_choke = flow.CHOKE_RATIO * p1
    just_above = flow.mass_flow(1.0, p1, p_choke + 1e-6, O2, T)
    just_below = flow.mass_flow(1.0, p1, p_choke - 1e-6, O2, T)
    assert just_above == pytest.approx(just_below, rel=2e-4)


def test_choked_flow_is_independent_of_backpressure():
    p1 = 400.0
    a = flow.mass_flow(1.0, p1, 100.0, O2, T)
    b = flow.mass_flow(1.0, p1, 50.0, O2, T)
    c = flow.mass_flow(1.0, p1, 1.0, O2, T)
    assert a == pytest.approx(b) == pytest.approx(c)
    assert a == pytest.approx(flow.choked_mass_flow(1.0, p1, O2, T))


def test_regime_classification():
    assert flow.regime_for(400.0, 300.0) is flow.Regime.SUBSONIC
    assert flow.regime_for(400.0, 200.0) is flow.Regime.CHOKED
    assert flow.regime_for(400.0, 100.0) is flow.Regime.CHOKED
    assert flow.regime_for(100.0, 400.0) is flow.Regime.NO_FLOW
    assert flow.regime_for(100.0, 100.0) is flow.Regime.NO_FLOW


def test_no_flow_without_pressure_difference():
    assert flow.mass_flow(1.0, 200.0, 200.0, O2, T) == 0.0
    assert flow.mass_flow(1.0, 200.0, 300.0, O2, T) == 0.0


def test_shut_component_passes_nothing():
    assert flow.mass_flow(0.0, 500.0, 100.0, O2, T) == 0.0


def test_mass_flow_is_linear_in_cv():
    base = flow.mass_flow(1.0, 400.0, 300.0, O2, T)
    assert flow.mass_flow(2.5, 400.0, 300.0, O2, T) == pytest.approx(2.5 * base)


@pytest.mark.parametrize("p2", [300.0, 200.0, 164.7, 50.0])
def test_p1_inverse_round_trips(p2):
    mdot = 0.05
    p1 = flow.p1_for_mass_flow(mdot, 0.9, p2, O2, T)
    assert flow.mass_flow(0.9, p1, p2, O2, T) == pytest.approx(mdot, rel=1e-9)


def test_p2_inverse_round_trips_in_subsonic_regime():
    p1 = 400.0
    target = flow.mass_flow(1.2, p1, 350.0, O2, T)
    p2 = flow.p2_for_mass_flow(target, 1.2, p1, O2, T)
    assert p2 == pytest.approx(350.0, rel=1e-6)


def test_p2_inverse_returns_choke_point_when_flow_is_choked():
    p1 = 400.0
    target = flow.choked_mass_flow(1.2, p1, O2, T)
    assert flow.p2_for_mass_flow(target, 1.2, p1, O2, T) == pytest.approx(200.0)


def test_p2_inverse_rejects_unreachable_flow():
    p1 = 400.0
    target = 2.0 * flow.choked_mass_flow(1.2, p1, O2, T)
    with pytest.raises(ValueError, match="choked"):
        flow.p2_for_mass_flow(target, 1.2, p1, O2, T)


def test_p1_inverse_rejects_impossible_demand():
    with pytest.raises(ValueError, match="cannot pass"):
        flow.p1_for_mass_flow(1e6, 0.1, 164.7, O2, T, p1_max_psia=1e4)


def test_cv_inverse_round_trips():
    cv = flow.cv_for_mass_flow(0.11, 418.2, 164.7, O2, T)
    assert flow.mass_flow(cv, 418.2, 164.7, O2, T) == pytest.approx(0.11, rel=1e-9)


def test_signed_mass_flow_is_antisymmetric():
    fwd = flow.signed_mass_flow(1.0, 400.0, 200.0, O2, T)
    rev = flow.signed_mass_flow(1.0, 200.0, 400.0, O2, T)
    assert fwd > 0 and rev < 0
    assert fwd == pytest.approx(-rev)


def test_check_valve_blocks_reverse_flow():
    assert flow.signed_mass_flow(1.0, 200.0, 400.0, O2, T, allow_reverse=False) == 0.0
    assert flow.signed_mass_flow(1.0, 400.0, 200.0, O2, T, allow_reverse=False) > 0.0


def test_heavier_gas_flows_less_mass_at_equal_volumetric_conditions():
    """Same Cv and pressures: O2 passes more mass than CH4 despite lower SCFH."""
    q_o2, _ = flow.scfh_from_cv(1.0, 400.0, 300.0, O2.Sg, T)
    q_ch4, _ = flow.scfh_from_cv(1.0, 400.0, 300.0, CH4.Sg, T)
    assert q_ch4 > q_o2  # lighter gas, more standard volume
    assert flow.mass_flow(1.0, 400.0, 300.0, O2, T) > flow.mass_flow(
        1.0, 400.0, 300.0, CH4, T
    )


def test_reference_density_does_not_track_flowing_temperature():
    """The SCFH reference state is fixed; only the valve equation sees local T."""
    cold = flow.mass_flow(1.0, 400.0, 300.0, O2, t_flow_R=400.0, t_ref_R=T)
    warm = flow.mass_flow(1.0, 400.0, 300.0, O2, t_flow_R=600.0, t_ref_R=T)
    assert cold > warm  # colder gas is denser in the sizing equation
    assert flow.reference_density(O2, T) == flow.reference_density(O2, T)


def test_series_cv_is_dominated_by_the_smallest_element():
    cv = flow.combine_series_cv([10.8, 9.3, 0.5])
    assert cv < 0.5
    assert cv == pytest.approx(0.49875, rel=1e-3)


def test_series_cv_with_shut_element_is_zero():
    assert flow.combine_series_cv([10.0, 0.0, 5.0]) == 0.0


def test_series_cv_requires_at_least_one_component():
    with pytest.raises(ValueError):
        flow.combine_series_cv([])
