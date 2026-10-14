"""Successful CDS jobs still need complete, unambiguous requested time labels."""
import hashlib

import numpy as np
import pytest

from climate_reanalysis.period import month_hours
from climate_reanalysis.raw_validation import time_axis_report
from tests.conftest import make_raw_nc


def raw(tmp_path, time):
    return make_raw_nc(tmp_path / '2m_temperature.nc', '202609', '2m_temperature',
                       time=time, lat=np.array([15., 15.25]), lon=np.array([70., 70.25]))


@pytest.mark.parametrize('kind', ['tail', 'middle', 'empty'])
def test_missing_hours_have_read_only_evidence(tmp_path, kind):
    expected = month_hours('202609')
    actual = {'tail': expected[:-23],
              'middle': np.concatenate([expected[:300], expected[323:]]),
              'empty': expected[:0]}[kind]
    path = raw(tmp_path, actual)
    before = hashlib.sha256(path.read_bytes()).digest()
    report = time_axis_report(path, '202609')
    assert report['expected_hours'] == 720
    assert report['observed_hours'] == len(actual)
    assert report['missing_hours'] == 720 - len(actual)
    if kind == 'tail':
        assert report['first_missing'] == '2026-09-30T01'
        assert report['last_missing'] == '2026-09-30T23'
    assert hashlib.sha256(path.read_bytes()).digest() == before


@pytest.mark.parametrize('kind', ['shifted', 'duplicate'])
def test_ambiguous_time_axes_are_not_treated_as_availability_delays(tmp_path, kind):
    expected = month_hours('202609')
    actual = (expected + np.timedelta64(1, 'h') if kind == 'shifted'
              else np.concatenate([expected[:-1], expected[:1]]))
    with pytest.raises(ValueError, match='拒绝自动重下'):
        time_axis_report(raw(tmp_path, actual), '202609')


def test_complete_unsorted_time_labels_match_restores_sorting_contract(tmp_path):
    assert time_axis_report(raw(tmp_path, month_hours('202609')[::-1]), '202609') is None
