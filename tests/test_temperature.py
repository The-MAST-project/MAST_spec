"""`cameras.temperature`: the reading, the set-point tolerance, and the FITS cards.

The one part of the highspec cameras' temperature handling that imports off a spec machine:
the drivers themselves load their vendor SDKs at import (MAST_spec#77), so they are covered
by a run on the hardware, not here.
"""

from __future__ import annotations

from cameras.temperature import (
    SET_POINT_TOLERANCE_DEGREES,
    TemperatureReading,
    is_at_set_point,
    temperature_cards,
)
from common.models.statuses import CameraTemperatureState

# A card spends 33 characters on `KEYWORD = <20-wide value> / `, out of 80.
MAX_COMMENT_LENGTH = 47


def _reading(value: float = -60.123, state: CameraTemperatureState | None = CameraTemperatureState.STABILIZED):
    return TemperatureReading(value=value, state=state, at="2026-10-20T08:00:00+00:00")


def test_the_tolerance_is_the_deepspec_cameras_one_degree():
    assert SET_POINT_TOLERANCE_DEGREES == 1.0


def test_at_set_point_within_the_tolerance_either_side():
    assert is_at_set_point(-59.0, -60)
    assert is_at_set_point(-61.0, -60)
    assert not is_at_set_point(-58.9, -60)


def test_never_at_a_set_point_that_was_not_given_or_without_a_value():
    assert not is_at_set_point(-60.0, None)
    assert not is_at_set_point(None, -60)


def test_a_reading_takes_its_time_now():
    reading = TemperatureReading.now(value=-60.0, state=CameraTemperatureState.DRIFT)
    assert reading.at.endswith("+00:00")


def test_all_three_cards_with_a_reading_and_a_set_point():
    cards = temperature_cards(_reading(), set_point=-60)
    assert [(k, v) for k, v, _ in cards] == [("CCD-TEMP", -60.12), ("SET-TEMP", -60), ("TEMPSTAT", "stabilized")]


def test_no_reading_writes_only_the_set_point():
    assert [k for k, _, _ in temperature_cards(None, set_point=-60)] == ["SET-TEMP"]


def test_no_set_point_writes_no_set_temp():
    assert [k for k, _, _ in temperature_cards(_reading(), set_point=None)] == ["CCD-TEMP", "TEMPSTAT"]


def test_a_reading_without_a_state_writes_no_tempstat():
    assert [k for k, _, _ in temperature_cards(_reading(state=None), set_point=-12.5)] == ["CCD-TEMP", "SET-TEMP"]


def test_every_comment_fits_its_card():
    for _, _, comment in temperature_cards(_reading(), set_point=-60):
        assert len(comment) <= MAX_COMMENT_LENGTH, comment
