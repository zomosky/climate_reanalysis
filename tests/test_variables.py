"""产出变量 ↔ CDS 变量的映射与换算。"""

from __future__ import annotations

import numpy as np
import pytest

from climate_reanalysis.variables import VAR_SPECS, cds_variables, convert


def test_nine_output_variables_match_store_contract():
    """产出变量集必须与 zrread 已在读的月库逐字一致。"""
    assert set(VAR_SPECS) == {
        "ssrd", "fdir", "ssrdc", "t2m", "d2m", "si10", "u100", "v100", "sp"
    }


def test_accumulated_flux_divided_by_3600():
    """J/m² -> W/m²，且 kind 为 avg（标签在区间右边界）。"""
    for name in ("ssrd", "fdir", "ssrdc"):
        spec = VAR_SPECS[name]
        assert spec.step_type == "avg"
        assert spec.units == "W m**-2"
        got = convert(name, {spec.cds[0]: np.array([3600.0, 7200.0])})
        np.testing.assert_allclose(got, [1.0, 2.0])


def test_instant_variables_pass_through_in_si():
    """瞬时量原样（K / m/s / Pa），不做任何换算。"""
    for name in ("t2m", "d2m", "u100", "v100", "sp"):
        spec = VAR_SPECS[name]
        assert spec.step_type == "instant"
        src = np.array([280.0, 290.0])
        np.testing.assert_allclose(convert(name, {spec.cds[0]: src}), src)


def test_si10_is_hypot_of_two_cds_variables():
    """一个产出变量可以来自两个 CDS 变量——manifest 粒度按产出变量。"""
    assert cds_variables("si10") == (
        "10m_u_component_of_wind", "10m_v_component_of_wind",
    )
    got = convert("si10", {"10m_u_component_of_wind": np.array([3.0]),
                           "10m_v_component_of_wind": np.array([4.0])})
    np.testing.assert_allclose(got, [5.0])


def test_negative_flux_clipped_to_zero():
    """累积量差分的数值噪声可能翻负；辐照不得为负。"""
    np.testing.assert_allclose(
        convert("ssrd", {"surface_solar_radiation_downwards": np.array([-36.0])}),
        [0.0],
    )


def test_unknown_variable_raises():
    with pytest.raises(KeyError):
        cds_variables("nope")
