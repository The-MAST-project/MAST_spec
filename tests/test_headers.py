"""`cameras.headers.update_header`, on files shaped like the Newton's `SaveAsFITS` output."""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from cameras.headers import update_header


@pytest.fixture
def andor_frame(tmp_path):
    """Unsigned 16-bit pixels stored as int16 + BZERO 32768, as the SDK writes them, with its
    own temperature cards: -999 in the one that does not apply."""
    path = tmp_path / "frame.fits"
    pixels = np.array([[0, 1, 32768], [40000, 65534, 65535]], dtype=np.uint16)
    hdu = fits.PrimaryHDU(pixels)
    hdu.header["TEMP"] = (-999.0, "Temperature")
    hdu.header["UNSTTEMP"] = (-40.956, "Unstabilized Temperature")
    hdu.writeto(path)
    return path, pixels


def _raw(path):
    with fits.open(path, do_not_scale_image_data=True) as hdul:
        return hdul[0].header.copy(), hdul[0].data.copy()


def test_the_cards_are_set_and_the_superseded_ones_removed(andor_frame):
    path, _ = andor_frame
    update_header(
        path,
        [("CCD-TEMP", -40.96, "[C] sensor temperature after exposure"), ("EXPREQ", 5.0, "[s] exposure requested")],
        remove=("TEMP", "UNSTTEMP"),
    )
    header = fits.getheader(path)
    assert header["CCD-TEMP"] == -40.96
    assert header.comments["CCD-TEMP"] == "[C] sensor temperature after exposure"
    assert header["EXPREQ"] == 5.0
    assert "TEMP" not in header
    assert "UNSTTEMP" not in header


def test_the_pixels_and_their_scaling_are_untouched(andor_frame):
    path, pixels = andor_frame
    raw_header_before, raw_before = _raw(path)

    update_header(path, [("CCD-TEMP", -40.96, "[C] sensor temperature after exposure")], remove=("TEMP",))

    raw_header_after, raw_after = _raw(path)
    assert raw_header_after["BZERO"] == raw_header_before["BZERO"] == 32768
    assert raw_header_after["BITPIX"] == raw_header_before["BITPIX"] == 16
    assert np.array_equal(raw_after, raw_before)
    assert np.array_equal(fits.getdata(path), pixels)


def test_a_keyword_to_remove_that_is_absent_is_skipped(andor_frame):
    path, _ = andor_frame
    update_header(path, [], remove=("NOSUCHKEY", "TEMP"))
    header = fits.getheader(path)
    assert "TEMP" not in header
    assert "UNSTTEMP" in header


def test_nothing_to_remove_keeps_the_sdk_cards(andor_frame):
    path, _ = andor_frame
    update_header(path, [("EXPREQ", 5.0, "[s] exposure requested")])
    header = fits.getheader(path)
    assert header["TEMP"] == -999.0
    assert header["UNSTTEMP"] == -40.956


def test_a_missing_file_raises_for_the_caller_to_handle(tmp_path):
    with pytest.raises(FileNotFoundError):
        update_header(tmp_path / "absent.fits", [("EXPREQ", 5.0, "[s] exposure requested")])
