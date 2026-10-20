"""Our own cards on a FITS file a camera SDK has already written.

Pure: astropy only, so it imports off a spec machine and is tested on real files.
"""

from __future__ import annotations

from pathlib import Path

from astropy.io import fits

Card = tuple[str, float | str, str]  # (keyword, value, comment)


def update_header(path: str | Path, cards: list[Card], remove: tuple[str, ...] = ()) -> None:
    """Set `cards` and delete the `remove` keywords on the primary header, in one rewrite.

    A keyword in `remove` that is absent is skipped. The data is never read or rescaled:
    `do_not_scale_image_data` keeps the SDK's BZERO / BSCALE and pixels exactly as it wrote
    them. Raises what astropy raises; the caller decides what a failure means.
    """
    with fits.open(path, mode="update", do_not_scale_image_data=True) as hdul:
        header = hdul[0].header
        for keyword, value, comment in cards:
            header[keyword] = (value, comment)
        for keyword in remove:
            if keyword in header:
                del header[keyword]
