import ctypes
import datetime
import os
import threading
import time
from collections.abc import Callable
from enum import IntFlag, auto
from pathlib import Path
from typing import Annotated, Literal

import zaber_motion
from fastapi import Query
from pydantic import BaseModel

from cameras.greateyes.greateyes import Exposure
from cameras.temperature import TemperatureReading, is_at_set_point, temperature_cards
from common.activities import HighspecActivities
from common.canonical import CanonicalResponse
from common.config import Config
from common.dlipowerswitch import SwitchedOutlet
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.statuses import CameraTemperatureState, QHY600Status
from common.paths import PathMaker
from common.spec import FrameType, SpecExposureSettings
from common.utils import RepeatTimer

from .controls import QHYControlId, qhy_controls
from .prototypes import set_ctypes_prototypes

qhy = ctypes.CDLL(os.path.join(os.path.dirname(__file__), "sdk", "2024-12-26-stable", "x64", "qhyccd.dll"))
set_ctypes_prototypes(qhy)

QHYCCD_SUCCESS = 0
STR_BUFFER_SIZE = 32
assert qhy is not None, "Failed to load QHY SDK"

logger = get_logger(__name__)


class QHYReadMode(IntFlag):  # detected from a QHY600U3 camera
    Photographic_DSO_16BIT = 0
    High_Gain_Mode_16BIT = 1
    Extend_Fullwell_Mode = 2
    Extended_Fullwell_2CMS = 3
    Bit14_MODE = 4  # fiber
    Bin3x3Mode = 5  # hardware
    Bit12Mode = 6  # fiber
    Bit12_raw_mode = 7  # fiber
    CMS2_0 = 8
    CMS2_1 = 9
    Bit14_mode_high_gain = 10
    Bit14_mode_low_noise = 11  # fiber


class QHYStreamMode(IntFlag):
    SingleFrame = 0
    Continuous = 1


class QHYRoiModel(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


class QHYBinningModel(BaseModel):
    x: int = 1
    y: int = 1


class QHYActivities(IntFlag):
    Idle = auto()
    Acquiring = auto()
    ExposingSingleFrame = auto()
    SettingParameters = auto()
    ReadingOut = auto()
    Saving = auto()
    # Appended, not inserted: auto() numbers by position, so a member added anywhere else
    # renumbers everything after it. This enum is local to this module rather than in
    # common.activities, so the blast radius is smaller than the Newton's and greateyes' --
    # but ComponentStatus.activities still carries the raw bitmask on the wire.
    Aborting = auto()
    # A set point given through `adjust_temperature`, until on_timer sees the sensor reach it.
    AdjustingTemperature = auto()


#: How often on_timer looks at the temperature while AdjustingTemperature is active.
TEMPERATURE_CHECK_SECONDS = 5


class QHYCameraSettingsModel(BaseModel):
    binning: QHYBinningModel = QHYBinningModel(x=1, y=1)
    roi: QHYRoiModel | None = None
    gain: int | None = None
    exposure_duration: float = 1.0  # in seconds
    number_of_exposures: int = 1
    image_path: str | Path | None = None  # full path to save image
    depth: Literal[8, 16] = 16  # bits per pixel
    # Recorded as IMAGETYP. This camera has no shutter to drive from it.
    frame_type: FrameType = FrameType.LIGHT


class QHY600(Component, SwitchedOutlet):
    """
    QHY600 camera control class.
    """

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        Component.__init__(self, QHYActivities)
        # SwitchedOutlet.__init__(
        #     self, domain=OutletDomain.SpecOutlets, outlet_name="QHY600U3"
        # )

        qhy.InitQHYCCDResource()
        logger.info(f"running in '{os.path.realpath(os.curdir)}'")

        self.cam_id = None
        self.serial_number = None
        self.handle = None
        self.chip_width = ctypes.c_double()
        self.chip_height = ctypes.c_double()
        self.width = ctypes.c_uint32()
        self.height = ctypes.c_uint32()
        self.pixel_width = ctypes.c_double()
        self.pixel_height = ctypes.c_double()
        self.channels = ctypes.c_uint32()
        self.bits_per_pixel = ctypes.c_uint32()
        self.bits_mode = 16
        self.fw_version = ctypes.create_string_buffer(32)
        self.fpga_version = (ctypes.c_uint8 * 32)()
        self.model = None
        self.parent_spec = None

        self.stop_event = threading.Event()
        self.read_modes: list[str] = []
        self.latest_settings: QHYCameraSettingsModel | None = None
        self._img_buffer = None
        self.supported_binnings: list[int] = []
        self.trigger_interfaces: list[str] = []

        self.errors: list[str] = []
        self.latest_exposure: Exposure | None = None
        self.latest_spec_exposure_settings: SpecExposureSettings | None = None

        # The set point last given through adjust_temperature, or None if never: the camera
        # is then cooling to whatever it was given before this process started.
        self._set_point: float | None = None
        # The latest temperature reading, which /status reports during an exposure, when the
        # camera is not read; and the one taken after the exposure for the frame's header.
        self.latest_temperature: TemperatureReading | None = None
        self.post_exposure_temperature: TemperatureReading | None = None
        # What the camera took CONTROL_EXPOSURE as, read back when the exposure is set up, and
        # when the exposure started: EXPOSURE and DATE-OBS in the header.
        self.actual_exposure_duration: float | None = None
        self.exposure_started_at: str | None = None

        self._connected = False
        self.connect()
        self._initialized = True

        # The QHY SDK signals nothing when the sensor reaches its set point, so this looks.
        self.timer = RepeatTimer(TEMPERATURE_CHECK_SECONDS, function=self.on_timer)
        self.timer.name = "qhy600-timer-thread"
        self.timer.start()

    def set_parent_spec(self, parent_spec):
        self.parent_spec = parent_spec

    @property
    def conf(self):
        """Highspec's camera settings, read live. Was `highspec.camera` -- the camera's NAME, a
        string -- so the `camera_enabled` check below never found the field."""
        return Config().get_specs().highspec.settings

    @property
    def connected(self) -> bool:
        return self.handle is not None

    @connected.setter
    def connected(self, value: bool):
        if value and not self.connected:
            self.connect()
        elif not value and self.connected:
            self.disconnect()

    def connect(self):
        self.initialize_camera()

    def __del__(self):
        if getattr(self, "timer", None) is not None:
            self.timer.cancel()
        self.disconnect()

    def disconnect(self):
        if qhy is not None:
            self.info("Disconnecting")
            if qhy and self.handle:
                qhy.CloseQHYCCD(self.handle)
            qhy.ReleaseQHYCCDResource()
        self.handle = None
        self.cam_id = None
        self.model = None
        self.serial_number = None
        self._connected = False

    def detect_suppoted_binnings(self):
        if not self.connected:
            self.error("Camera not connected.")
            return

        self.supported_binnings.clear()
        for binning in [1, 2, 3, 4]:
            if self.sdk_call(qhy.SetQHYCCDBinMode, binning, binning, silent=True) == QHYCCD_SUCCESS:
                self.supported_binnings.append(binning)

        if not self.supported_binnings:
            self.warning("No supported binnings detected.")
        else:
            self.info(f"Supported binnings: {self.supported_binnings}")

    def detect_read_modes(self):
        if not self.connected:
            self.error("Camera not connected.")
            return

        self.read_modes.clear()
        nmodes = ctypes.c_uint32()
        if qhy.GetReadModesNumber(self.cam_id, ctypes.byref(nmodes)) != QHYCCD_SUCCESS:
            self.error("Failed to get number of read modes.")
            return

        buffer = ctypes.create_string_buffer(STR_BUFFER_SIZE)
        mode = ctypes.c_uint32()
        for m in range(nmodes.value):
            mode = ctypes.c_uint32(m)
            if self.sdk_call(qhy.GetQHYCCDReadModeName, mode, buffer, silent=True) == QHYCCD_SUCCESS:
                self.read_modes.append(buffer.value.decode("utf-8"))

        if not self.read_modes:
            self.warning("No read modes detected.")
        else:
            for i in range(len(self.read_modes)):
                self.debug(f"Read mode {i:2}: {self.read_modes[i]}")

    def set_trigger_out(self):
        """
        Detects and sets the trigger output interface to GPIO if available.
        """
        if not self.connected:
            self.error("Camera not connected.")
            return

        if self.sdk_call(qhy.IsQHYCCDControlAvailable, QHYControlId.CAM_TRIGER_OUT, silent=True) != QHYCCD_SUCCESS:
            self.debug("control 'CAM_TRIGER_OUT' not available")
            return

        self.debug("control 'CAM_TRIGER_OUT' is available")
        if (
            self.sdk_call(
                qhy.IsQHYCCDControlAvailable,
                QHYControlId.CAM_TRIGER_INTERFACE,
                silent=True,
            )
            != QHYCCD_SUCCESS
        ):
            self.debug("control CAM_TRIGER_INTERFACE not available")
            return

        ninterfaces = ctypes.c_uint32()
        if (
            self.sdk_call(
                qhy.GetQHYCCDTrigerInterfaceNumber,
                ctypes.byref(ninterfaces),
                silent=True,
            )
            != QHYCCD_SUCCESS
        ):
            self.error("Failed to get number of trigger intrfaces")
            return

        interface_name = ctypes.create_string_buffer(STR_BUFFER_SIZE)
        for i in range(ninterfaces.value):
            if (
                self.sdk_call(
                    qhy.GetQHYCCDTrigerInterfaceName,
                    ctypes.c_uint32(i),
                    interface_name,
                    silent=True,
                )
                == QHYCCD_SUCCESS
            ):
                decoded_name = interface_name.value.decode("utf-8")
                self.trigger_interfaces.append(decoded_name)
                if "gpio" in decoded_name.lower():
                    self.debug(f"selecting trigger interface {i} ('{decoded_name}')")
                    if self.sdk_call(qhy.SetQHYCCDTrigerInterface, i, silent=True) != QHYCCD_SUCCESS:
                        self.error(f"failed to select trigger interface {i} ('{decoded_name}')")

                    self.sdk_call(qhy.EnableQHYCCDTrigerOut)
                    break

    def initialize_camera(self):
        assert qhy is not None, "Failed to load QHY SDK"
        ncam = qhy.ScanQHYCCD()
        self.info(f"QHY: found: {ncam} camera(s)")
        if ncam == 0:
            return

        sn = ctypes.create_string_buffer(64)
        assert qhy.GetQHYCCDId(0, sn) == 0
        s = sn.value.decode("utf-8")
        self.model, _, self.serial_number = s.partition("-")

        self.cam_id = sn
        self.handle = qhy.OpenQHYCCD(sn)
        assert self.handle is not None, "Failed to open camera."

        if self.sdk_call(qhy.InitQHYCCD) != QHYCCD_SUCCESS:
            return

        if (
            self.sdk_call(
                qhy.GetQHYCCDChipInfo,
                ctypes.byref(self.chip_width),
                ctypes.byref(self.chip_height),
                ctypes.byref(self.width),
                ctypes.byref(self.height),
                ctypes.byref(self.pixel_width),
                ctypes.byref(self.pixel_height),
                ctypes.byref(self.bits_per_pixel),
                silent=True,
            )
            == QHYCCD_SUCCESS
        ):
            self.debug(
                f"chip info: {self.chip_width.value}mm x {self.chip_height.value}mm, "
                f"{self.width.value} x {self.height.value} pixels, "
                f"{self.pixel_width.value}um x {self.pixel_height.value}um pixels, "
                f"{self.bits_per_pixel.value} bits per pixel"
            )
        else:
            self.error("Failed to get chip info.")
            return

        year = ctypes.c_uint32()
        month = ctypes.c_uint32()
        day = ctypes.c_uint32()
        subday = ctypes.c_uint32()

        if (
            ret := qhy.GetQHYCCDSDKVersion(
                ctypes.byref(year),
                ctypes.byref(month),
                ctypes.byref(day),
                ctypes.byref(subday),
            )
        ) == QHYCCD_SUCCESS:
            self.debug(f"SDK version: year=20{year.value} month={month.value:02} day={day.value:02}")
        else:
            self.warning(f"Failed to get SDK version {ret=}")

        v = (ctypes.c_uint8 * 32)()
        if self.sdk_call(qhy.GetQHYCCDFPGAVersion, 0, v, silent=True) == QHYCCD_SUCCESS:
            self.fpga_version = f"{v[0]}-{v[1]}-{v[2]}-{v[3]}"
            self.debug(f"FPGA version: {self.fpga_version}")

        v = (ctypes.c_uint8 * 32)()
        if self.sdk_call(qhy.GetQHYCCDFWVersion, v, silent=True) == QHYCCD_SUCCESS:
            if (v[0] >> 4) <= 9:
                self.fw_version = f"{(v[0] >> 4) + 0x10}-{v[0] & ~0xF0}-{v[1]}"
            else:
                self.fw_version = f"{v[0] >> 4}-{v[0] & ~0xF0}-{v[1]}"
            self.debug(f"FW version: {self.fw_version}")

        self.detect_suppoted_binnings()
        # self.detect_read_modes()
        self.set_trigger_out()

    def info(self, message):
        if self.model:
            message = f"{self.model}: {message}"
        logger.info(message)

    def warning(self, message):
        if self.model:
            message = f"{self.model}: {message}"
        logger.warning(message)

    def error(self, message):
        if self.model:
            message = f"{self.model}: {message}"
        logger.error(message)

    def debug(self, message):
        if self.model:
            message = f"{self.model}: {message}"
        logger.debug(message)

    def sdk_call(self, func: Callable, *args, silent=False):
        if not self.connected:
            self.error("Camera not connected.")
            return None

        signature = f"{func.__name__}({[f'{arg}' for arg in args]})".replace("[", "").replace("]", "")

        try:
            ret = func(self.handle, *args)
            if func.__name__ != "GetQHYCCDMemLength" and ret != QHYCCD_SUCCESS:
                self.error(f"SDK function '{signature}' failed with error code {hex(ret)}")
                return None
            if not silent:
                self.debug(f"SDK function {signature} returned {ret}")
            return ret
        except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
            self.error(f"SDK function {signature}: {e=}")
            return None

    def sdk_get_control(self, control_id: ctypes.c_int) -> float | None:
        if not self.connected:
            self.error("Camera not connected.")
            return None
        try:
            assert qhy is not None, "QHY SDK not loaded"
            value = qhy.GetQHYCCDParam(self.handle, control_id)
            self.debug(f"SDK get control {control_id} returned {value}")
            return value
        except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
            self.error(f"Error getting control {control_id}: {e}")
            return None

    def sdk_set_control(self, control_id: ctypes.c_int, value: ctypes.c_double) -> bool:
        if not self.connected:
            self.error("Camera not connected.")
            return False
        try:
            assert qhy is not None, "QHY SDK not loaded"
            found = [ctrl for ctrl in qhy_controls if ctrl.id == control_id]
            if not found:
                self.error(f"Control ID {control_id} not recognized.")
                return False
            control = found[0]

            if control.range is not None:
                min_val, max_val = control.range.min, control.range.max
                if not (min_val <= value.value <= max_val):
                    self.error(f"Value {value} for control '{control.name}' out of range ({min_val}, {max_val})")
                    return False
            if (ret := qhy.SetQHYCCDParam(self.handle, control.id, value)) != QHYCCD_SUCCESS:
                self.error(f"Failed to set control {control.name} to {value}: error code {ret}")
                return False
            self.debug(f"SDK set control {control.name} to {value}")
            return True
        except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
            self.error(f"Error setting control {control.name} to {value}: {e=}")
            return False

    def start_single_exposure(self, settings: QHYCameraSettingsModel):
        if qhy is None or self.handle is None:
            self.error("Camera not connected.")
            return

        if settings.binning.x not in self.supported_binnings:
            self.error(f"Binning {settings.binning.x} not supported.")
            return

        control = next((c for c in qhy_controls if c.id == QHYControlId.CONTROL_GAIN), None)
        if (
            control is not None
            and control.range is not None
            and settings.gain is not None
            and not (control.range.min <= settings.gain <= control.range.max)
        ):
            self.error(f"Gain setting {settings.gain} out of range {control.range.min}..{control.range.max}")
            return

        if self.sdk_call(qhy.CancelQHYCCDExposingAndReadout) != QHYCCD_SUCCESS:  # cancel any ongoing exposure
            return

        self.start_activity(QHYActivities.ExposingSingleFrame)
        self.start_activity(QHYActivities.SettingParameters)

        # These three returned with ExposingSingleFrame still set, so a failed setup left the
        # camera looking busy for good -- and /status, which no longer reads the temperature
        # while it is set, would have reported a stale one from then on.
        read_mode = QHYReadMode.Photographic_DSO_16BIT
        if self.sdk_call(qhy.SetQHYCCDReadMode, read_mode) != QHYCCD_SUCCESS:
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        stream_mode = QHYStreamMode.SingleFrame
        if self.sdk_call(qhy.SetQHYCCDStreamMode, stream_mode) != QHYCCD_SUCCESS:
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        if self.sdk_call(qhy.SetQHYCCDBitsMode, self.bits_mode) != QHYCCD_SUCCESS:
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        self.latest_settings = settings
        self.sdk_set_control(
            QHYControlId.CONTROL_EXPOSURE,
            ctypes.c_double(settings.exposure_duration * 1e6),
        )
        # What the camera took it as, for EXPOSURE: the header used to carry the request only.
        exposure_us = self.sdk_get_control(QHYControlId.CONTROL_EXPOSURE)
        self.actual_exposure_duration = exposure_us / 1e6 if exposure_us is not None else None

        if settings.gain is not None and not self.sdk_set_control(QHYControlId.CONTROL_GAIN, ctypes.c_double(settings.gain)):
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        if settings.depth in (8, 16) and not self.sdk_set_control(
            QHYControlId.CONTROL_TRANSFERBIT, ctypes.c_double(settings.depth)
        ):
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        if self.sdk_call(qhy.SetQHYCCDBinMode, settings.binning.x, settings.binning.y) != QHYCCD_SUCCESS:
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        binning = settings.binning.x  # assuming x and y are the same
        roi = settings.roi or QHYRoiModel(x=0, y=0, width=self.width.value, height=self.height.value)
        if (
            self.sdk_call(
                qhy.SetQHYCCDResolution,
                ctypes.c_uint32(roi.x // binning),
                ctypes.c_uint32(roi.y // binning),
                ctypes.c_uint32(int(roi.width // binning)),
                ctypes.c_uint32(int(roi.height // binning)),
                silent=True,
            )
            != QHYCCD_SUCCESS
        ):
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        if settings.gain is not None and not self.sdk_set_control(QHYControlId.CONTROL_GAIN, ctypes.c_double(settings.gain)):
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        self.end_activity(QHYActivities.SettingParameters)

        # Start exposure
        self.info(f"Starting exposure: {settings.exposure_duration:.2f}s")
        # DATE-OBS is when the exposure starts; it used to be when the file was saved.
        self.exposure_started_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        if self.sdk_call(qhy.ExpQHYCCDSingleFrame) != QHYCCD_SUCCESS:
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        completer = threading.Thread(name="qhy600-exposure-completer", target=self.complete_exposure)
        completer.start()

    def complete_exposure(self):
        if not self.is_active(QHYActivities.ExposingSingleFrame):
            self.error("not exposing")
            return

        width = ctypes.c_uint32()
        height = ctypes.c_uint32()
        bpp = ctypes.c_uint32()
        channels = ctypes.c_uint32()

        settings = self.latest_settings
        assert settings is not None, "No exposure settings available."

        npixels = self.width.value * self.height.value
        self._img_buffer = (ctypes.c_uint8 if settings.depth == 8 else ctypes.c_uint16 * npixels)()

        self.start_activity(QHYActivities.ReadingOut)
        if (
            self.sdk_call(
                qhy.GetQHYCCDSingleFrame,
                ctypes.byref(width),
                ctypes.byref(height),
                ctypes.byref(bpp),
                ctypes.byref(channels),
                ctypes.cast(self._img_buffer, ctypes.POINTER(ctypes.c_ubyte)),
                silent=True,
            )
            != QHYCCD_SUCCESS
        ):
            self.end_activity(QHYActivities.ExposingSingleFrame)
            self.end_activity(QHYActivities.ReadingOut)
            # A cancelled GetQHYCCDSingleFrame lands here, which is the intended path for an
            # abort: no frame was fetched, so there is nothing to write and nothing to discard.
            self.end_activity(QHYActivities.Aborting)
            return

        self.end_activity(QHYActivities.ReadingOut)
        self.info(f"Image acquired: {width.value}x{height.value}, {bpp.value} bpp, {channels.value} channels")

        # The frame is discarded, not written, if an abort arrived while this thread ran and
        # the SDK still handed back data.
        if self.is_active(QHYActivities.Aborting):
            self.info("aborted: discarding the frame instead of saving it")
            self.end_activity(QHYActivities.Aborting)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        # GetQHYCCDSingleFrame spans the exposure and the readout, so this is the first moment
        # after the exposure that the camera can be read.
        self.post_exposure_temperature = self.read_temperature()

        if self.latest_settings is not None and self.latest_settings.image_path is not None:
            self.start_activity(QHYActivities.Saving)

            import numpy as np
            from astropy.io import fits

            img_array = np.ctypeslib.as_array(self._img_buffer)
            img_array = img_array.reshape((height.value, width.value))

            hdu = fits.PrimaryHDU(img_array)
            hdu.header["SIMPLE"] = True
            if settings.depth == 16:
                hdu.header["BITPIX"] = 16
                hdu.header["BZERO"] = 32768
                hdu.header["BSCALE"] = 1
            else:
                hdu.header["BITPIX"] = 8

            hdu.header["NAXIS"] = 2
            if settings.roi is None:
                hdu.header["NAXIS1"] = self.width.value
                hdu.header["NAXIS2"] = self.height.value
            else:
                hdu.header["NAXIS1"] = settings.roi.width
                hdu.header["NAXIS2"] = settings.roi.height

            # The same exposure cards as the Newton's: what happened, and what was asked for.
            # EXPTIME stays, with the actual, because reduction tools look for it.
            requested = settings.exposure_duration
            actual = self.actual_exposure_duration if self.actual_exposure_duration is not None else requested
            hdu.header["EXPOSURE"] = (actual, "[s] actual exposure (CONTROL_EXPOSURE)")
            hdu.header["EXPTIME"] = (actual, "[s] actual exposure, as EXPOSURE")
            hdu.header["EXPREQ"] = (requested, "[s] exposure requested")
            hdu.header["IMAGETYP"] = (settings.frame_type.value, "frame type requested (light/bias/dark/flat)")

            # gain = self.sdk_get_control(QHYControlId.CONTROL_GAIN)
            # hdu.header["GAIN"] = gain if gain is not None else "unknown"
            if settings.roi is not None:
                hdu.header["ROI_X"] = settings.roi.x
                hdu.header["ROI_Y"] = settings.roi.y
                hdu.header["ROI_W"] = settings.roi.width
                hdu.header["ROI_H"] = settings.roi.height
            if settings.binning is not None:
                hdu.header["XBINNING"] = settings.binning.x
                hdu.header["YBINNING"] = settings.binning.y
            hdu.header["INSTRUME"] = self.model or "QHY600MM"
            hdu.header["DATE-OBS"] = (
                self.exposure_started_at or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "UTC start of exposure",
            )
            for keyword, value, comment in temperature_cards(self.post_exposure_temperature, self._set_point):
                hdu.header[keyword] = (value, comment)
            if self.post_exposure_temperature is None:
                self.warning("no temperature reading after the exposure: CCD-TEMP and TEMPSTAT not written")
            if self.parent_spec is not None:
                hdu.header["FOCUSPOS"] = self.parent_spec.focusing_stage.position(unit=zaber_motion.Units.LENGTH_MILLIMETRES)
                mnemonic = next(
                    k for k, v in zaber_motion.units.LITERALS_TO_UNITS.items() if v == zaber_motion.Units.LENGTH_MILLIMETRES
                )
                hdu.header["FOCUSUNI"] = mnemonic
            # hdu.header["GAIN"] = self.gain
            # Protect the FITS while it is being written, and record it as a product so
            # release_folder() waits for it rather than discarding it as scratch.
            with MoveGuardian().protect(str(self.latest_settings.image_path)):
                hdu.writeto(self.latest_settings.image_path, overwrite=True)
            self.info(f"{self.model}: Image saved to {self.latest_settings.image_path!s}")
            self.end_activity(QHYActivities.Saving)

        self.end_activity(QHYActivities.ReadingOut)
        self.end_activity(QHYActivities.ExposingSingleFrame)

        if self.parent_spec is not None and self.parent_spec.is_active(HighspecActivities.Exposing):
            self.parent_spec.end_activity(HighspecActivities.Exposing)

    def start_cooldown(self):
        pass  # camera does not have a cooling procedure, so do nothing

    def start_warmup(self):
        pass  # camera does not have a warming procedure, so do nothing

    def startup(self):
        if hasattr(self.conf, "camera_enabled") and not self.conf.camera_enabled:
            self.info("Camera is disabled.")
            return
        return super().startup()

    def shutdown(self):
        pass  # camera does not have a shutdown procedure, so do nothing

    @property
    def is_shutting_down(self) -> bool:
        return False  # does not have a shutdown procedure, so never report as shutting down

    def powerdown(self):
        self.power_off()

    def adjust_temperature(self, target_temperature: float) -> CanonicalResponse:
        """Move the cooler to a new set point; `AdjustingTemperature` until the sensor is there.

        Taken as given: CONTROL_COOLER is a double. Exposures are allowed meanwhile, but the
        set point is not changed during one, so a frame is never taken while it moves. A new
        target supersedes one still being adjusted to.
        """
        if not self.connected:
            return CanonicalResponse(errors=["camera not connected"])
        if self.is_active(QHYActivities.ExposingSingleFrame):
            return CanonicalResponse(errors=["cannot adjust the temperature during an exposure"])
        # sdk_set_control checks the value against the control's range, and logs a refusal.
        if not self.sdk_set_control(QHYControlId.CONTROL_COOLER, ctypes.c_double(target_temperature)):
            return CanonicalResponse(errors=[f"the camera refused set point {target_temperature} C"])

        self._set_point = target_temperature
        if self.is_active(QHYActivities.AdjustingTemperature):
            self.end_activity(QHYActivities.AdjustingTemperature)
        self.start_activity(QHYActivities.AdjustingTemperature, details=[f"set_point={target_temperature}"])
        # Already there, it ends now rather than on the next tick.
        self.read_temperature()
        return CanonicalResponse(value=f"set point {target_temperature} C")

    def read_temperature(self) -> TemperatureReading | None:
        """Read the sensor temperature. A reading at the set point ends AdjustingTemperature.

        The QHY SDK reports a temperature and no cooler state, so the state is this driver's
        own: `adjusting` until the sensor is within SET_POINT_TOLERANCE_DEGREES of the set
        point, `stabilized` there, `drift` if it later leaves it, and None when no set point
        has been given -- the camera is then cooling to something this process never chose.
        """
        if not self.connected:
            return None
        value = self.sdk_get_control(QHYControlId.CONTROL_CURTEMP)
        if value is None:
            return None

        at_set_point = is_at_set_point(value, self._set_point)
        if self.is_active(QHYActivities.AdjustingTemperature) and at_set_point:
            self.info(f"temperature has reached the set point: {value:.2f} C (set point {self._set_point})")
            self.end_activity(QHYActivities.AdjustingTemperature)

        if self._set_point is None:
            state = None
        elif self.is_active(QHYActivities.AdjustingTemperature):
            state = CameraTemperatureState.ADJUSTING
        else:
            state = CameraTemperatureState.STABILIZED if at_set_point else CameraTemperatureState.DRIFT

        reading = TemperatureReading.now(value=value, state=state)
        self.latest_temperature = reading
        return reading

    def _temperature_for_status(self) -> TemperatureReading | None:
        """A fresh reading, or -- during an exposure -- the latest one, without asking the SDK."""
        if self.is_active(QHYActivities.ExposingSingleFrame):
            return self.latest_temperature
        return self.read_temperature() or self.latest_temperature

    def on_timer(self):
        """Every TEMPERATURE_CHECK_SECONDS: end AdjustingTemperature once the sensor is there.

        RepeatTimer has no exception handling (MAST_common#106): one exception here would stop
        the timer silently for the life of the process, so nothing escapes.
        """
        try:
            if (
                self.connected
                and self.is_active(QHYActivities.AdjustingTemperature)
                and not self.is_active(QHYActivities.ExposingSingleFrame)
            ):
                self.read_temperature()
        except Exception as e:  # noqa: BLE001 -- the timer must survive any one tick
            self.error(f"on_timer: {e!r}")

    def status(self) -> QHY600Status | None:
        stat = None
        # with suppress(Exception):
        try:
            reading = self._temperature_for_status()
            stat = QHY600Status(
                # powered=self.is_on(),
                current_set_point=self._set_point,
                temperature=reading.value if reading else None,
                temperature_state=reading.state if reading else None,
                temperature_at=reading.at if reading else None,
                errors=self.errors,
                latest_exposure=self.latest_exposure,
                latest_spec_exposure_settings=self.latest_spec_exposure_settings,
            )
            return stat
        except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
            self.error(f"Error getting status: {e=}")
            return None

    @property
    def temperature_is_stabilized(self) -> bool:
        # Not while a set point given through adjust_temperature is still being reached.
        # With none given, as before: assumed stable, since nothing here chose a target to
        # compare against, and blocking exposures on that would block them for good.
        return not self.is_active(QHYActivities.AdjustingTemperature)

    @property
    def operational(self) -> bool:
        return self.detected and self.connected

    def abort(self):
        """Stop the exposure and readout, and see to it that no frame is written.

        This camera had no abort at all: the body was `return super().abort()`, which resolves
        through the MRO to `Component.abort` -- an abstract method whose body is a docstring.
        It returned None and did nothing, so the whole chain from the plan client down was a
        no-op here while every layer above reported success (MAST_spec#84).

        CancelQHYCCDExposingAndReadout rather than CancelQHYCCDExposing, because this camera
        does not separate the two phases the way the other two do: GetQHYCCDSingleFrame is a
        single blocking call spanning the exposure AND the readout, so cancelling only the
        exposure would leave that call waiting for a readout that is no longer coming.

        Cancelling makes it return non-success, and complete_exposure's existing failure branch
        then ends the activities without writing. The Aborting flag is belt and braces for the
        case where it returns success with a partial frame, and is what a caller can wait on.
        """
        if not self.detected:
            return

        self.start_activity(QHYActivities.Aborting)

        if self.handle is not None:
            self.sdk_call(qhy.CancelQHYCCDExposingAndReadout)

        # Nothing in flight to discard, so the interval is over as soon as it began -- the flag
        # means "a frame is still to be discarded", not "an abort happened".
        if not self.is_active(QHYActivities.ExposingSingleFrame) and not self.is_active(QHYActivities.ReadingOut):
            self.end_activity(QHYActivities.Aborting)

    @property
    def name(self) -> str:
        return "qhy600mm"

    @property
    def detected(self) -> bool:
        return self.connected

    @property
    def why_not_operational(self) -> list[str]:
        ret: list[str] = []
        if not self.detected:
            ret.append(f"{self.name}: not detected")
        elif not self.connected:
            ret.append(f"{self.name}: not connected")
        return ret

    @property
    def was_shut_down(self) -> bool:
        return False

    # Grouped and described the same way as NewtonEMCCD.expose_single_image, because
    # whichever camera this machine has is the one whose signature /docs shows.
    def expose_single_image(
        self,
        duration: Annotated[
            float,
            Query(description="**--- Exposure ---**\n\nExposure length (seconds)."),
        ],
        gain: Annotated[
            int | None,
            Query(
                description=(
                    "**--- Sensor ---**\n\n"
                    "QHY600 sensor gain. Unlike the Newton's gains this has no configured "
                    "fallback: omitted, the gain is simply not set and the camera keeps "
                    "whatever it had from the previous exposure. The legal range is the "
                    "camera's own CONTROL_GAIN range, checked when the exposure starts."
                )
            ),
        ] = None,
    ):
        folder = PathMaker().make_spec_exposures_folder(spec_name="highspec")
        seq = PathMaker().make_seq(folder=folder)
        settings = QHYCameraSettingsModel(
            binning=QHYBinningModel(x=1, y=1),
            roi=None,
            gain=gain,
            exposure_duration=duration,
            number_of_exposures=1,
            image_path=Path(folder) / f"single_exposure_{seq}.fits",
            depth=16,
        )
        self.start_single_exposure(settings)

        # This endpoint returns as soon as the exposure is started, so the move and the
        # release happen on a watcher rather than here. Polling ExposingSingleFrame is safe
        # precisely because start_single_exposure() set it on *this* thread before
        # returning -- unlike a flag a worker sets later, it cannot be read too early.
        image_path = str(settings.image_path)

        def wait_then_hand_over():
            while self.is_active(QHYActivities.ExposingSingleFrame):
                time.sleep(0.5)
            Filer().move_ram_to_shared(image_path)
            MoveGuardian().release_folder(folder, logger=logger)

        threading.Thread(name="qhy600-single-exposure-mover", target=wait_then_hand_over).start()

    def start_acquisition(self, spec_exposure_settings: SpecExposureSettings):
        settings: QHYCameraSettingsModel = QHYCameraSettingsModel(
            binning=QHYBinningModel(
                x=spec_exposure_settings.x_binning or 1,
                y=spec_exposure_settings.y_binning or 1,
            ),
            roi=None,
            # gain=spec_exposure_settings.gain or 100,
            exposure_duration=spec_exposure_settings.exposure_duration,
            number_of_exposures=spec_exposure_settings.number_of_exposures or 1,
            image_path=spec_exposure_settings.image_full_name,
            depth=16,
            frame_type=spec_exposure_settings.frame_type,
        )

        threading.Thread(
            name="qhy600-acquisition-thread",
            target=self.start_single_exposure,
            args=(settings,),
        ).start()


if __name__ == "__main__":
    camera = QHY600()
    if camera.connected:
        settings = QHYCameraSettingsModel(
            binning=QHYBinningModel(x=1, y=1),
            roi=None,
            gain=100,
            exposure_duration=1.0,
            number_of_exposures=1,
            image_path="test_image.fits",
            depth=16,
        )
        camera.start_single_exposure(settings)
    else:
        print("Camera not connected.")
