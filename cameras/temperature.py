"""Temperature readings of the highspec cameras, and the FITS cards that record them.

Pure: no SDK and no hardware, so it imports anywhere. The two drivers (`cameras.andor.newton`,
`cameras.qhy.qhy600`) turn their SDK's answers into a `TemperatureReading`, and both write
the same three cards with `temperature_cards`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from common.models.statuses import CameraTemperatureState

#: How close to the set point counts as "there". The same 1 C as the deepspec cameras'
#: AdjustingTemperature (`GreatEyes.on_timer`), so the word means one thing on every camera.
SET_POINT_TOLERANCE_DEGREES = 1.0


@dataclass(frozen=True)
class TemperatureReading:
    """One temperature reading, the cooler's state with it, and when it was taken."""

    value: float
    state: CameraTemperatureState | None
    at: str  # ISO UTC

    @classmethod
    def now(cls, value: float, state: CameraTemperatureState | None) -> TemperatureReading:
        return cls(value=value, state=state, at=datetime.datetime.now(datetime.UTC).isoformat())


def is_at_set_point(value: float | None, set_point: float | None) -> bool:
    if value is None or set_point is None:
        return False
    return abs(value - set_point) <= SET_POINT_TOLERANCE_DEGREES


def temperature_cards(reading: TemperatureReading | None, set_point: float | None) -> list[tuple[str, float | str, str]]:
    """The (keyword, value, comment) cards recording the temperature after an exposure.

    A card with nothing true to say is left out rather than written as a guess: no reading,
    no CCD-TEMP or TEMPSTAT; no set point, no SET-TEMP.

    Comments stay within 47 characters. A card spends 33 on `KEYWORD = <20-wide value> / `,
    and astropy truncates a longer comment with only a VerifyWarning to say so.
    """
    cards: list[tuple[str, float | str, str]] = []
    if reading is not None:
        cards.append(("CCD-TEMP", round(reading.value, 2), "[C] sensor temperature after exposure"))
    if set_point is not None:
        cards.append(("SET-TEMP", set_point, "[C] cooler set point"))
    if reading is not None and reading.state is not None:
        cards.append(("TEMPSTAT", reading.state.value, "cooler state after exposure"))
    return cards
