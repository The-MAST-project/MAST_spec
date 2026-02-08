import ctypes
import logging
import os
import threading
import time
from enum import IntFlag, auto
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from common.activities import HighspecActivities
from common.dlipowerswitch import SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.spec import SpecExposureSettings

from .controls import QHYControlId, qhy_controls
from .prototypes import set_ctypes_prototypes

qhy = ctypes.CDLL(
    os.path.join(
        os.path.dirname(__file__), "sdk", "2024-12-26-stable", "x64", "qhyccd.dll"
    )
)
set_ctypes_prototypes(qhy)

QHYCCD_SUCCESS = 0
STR_BUFFER_SIZE = 32
assert qhy is not None, "Failed to load QHY SDK"

logger = logging.getLogger(f"mast.highspec.{__name__}")
init_log(logger)


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


class QHYCameraSettingsModel(BaseModel):
    binning: QHYBinningModel = QHYBinningModel(x=1, y=1)
    roi: QHYRoiModel | None = None
    gain: int | None = None
    exposure_duration: float = 1.0  # in seconds
    number_of_exposures: int = 1
    image_path: str | Path | None = None  # full path to save image
    depth: Literal[8, 16] = 16  # bits per pixel


class QHY600(Component, SwitchedOutlet):
    """
    QHY600 camera control class.
    """

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(QHY600, cls).__new__(cls)
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

        self._connected = False
        self.connect()
        self._initialized = True

    def set_parent_spec(self, parent_spec):
        self.parent_spec = parent_spec

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
            if (
                self.sdk_call(qhy.SetQHYCCDBinMode, binning, binning, silent=True)
                == QHYCCD_SUCCESS
            ):
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
            if (
                self.sdk_call(qhy.GetQHYCCDReadModeName, mode, buffer, silent=True)
                == QHYCCD_SUCCESS
            ):
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

        if (
            self.sdk_call(
                qhy.IsQHYCCDControlAvailable, QHYControlId.CAM_TRIGER_OUT, silent=True
            )
            != QHYCCD_SUCCESS
        ):
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
                    if (
                        self.sdk_call(qhy.SetQHYCCDTrigerInterface, i, silent=True)
                        != QHYCCD_SUCCESS
                    ):
                        self.error(
                            f"failed to select trigger interface {i} ('{decoded_name}')"
                        )

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
                f"{self.width.value} x {self.height.value} pixels, {self.pixel_width.value}um x {self.pixel_height.value}um pixels, "
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
            self.debug(
                f"SDK version: year=20{year.value} month={month.value:02} day={day.value:02}"
            )
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

        signature = f"{func.__name__}({[f'{arg}' for arg in args]})".replace(
            "[", ""
        ).replace("]", "")

        try:
            ret = func(self.handle, *args)
            if func.__name__ != "GetQHYCCDMemLength" and ret != QHYCCD_SUCCESS:
                self.error(
                    f"SDK function '{signature}' failed with error code {hex(ret)}"
                )
                return None
            if not silent:
                self.debug(f"SDK function {signature} returned {ret}")
            return ret
        except Exception as e:
            self.error(f"SDK function {signature}: {e=}")
            return None

    def sdk_get_control(self, control_id: QHYControlId) -> float | None:
        if not self.connected:
            self.error("Camera not connected.")
            return None
        try:
            assert qhy is not None, "QHY SDK not loaded"
            value = qhy.GetQHYCCDParam(self.handle, control_id)
            self.debug(f"SDK get control {control_id} returned {value}")
            return value
        except Exception as e:
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
                    self.error(
                        f"Value {value} for control '{control.name}' out of range ({min_val}, {max_val})"
                    )
                    return False
            if (
                ret := qhy.SetQHYCCDParam(self.handle, control.id, value)
            ) != QHYCCD_SUCCESS:
                self.error(
                    f"Failed to set control {control.name} to {value}: error code {ret}"
                )
                return False
            self.debug(f"SDK set control {control.name} to {value}")
            return True
        except Exception as e:
            self.error(f"Error setting control {control.name} to {value}: {e=}")
            return False

    def start_single_exposure(self, settings: QHYCameraSettingsModel):
        if qhy is None or self.handle is None:
            self.error("Camera not connected.")
            return

        if settings.binning.x not in self.supported_binnings:
            self.error(f"Binning {settings.binning.x} not supported.")
            return

        control = next(
            (c for c in qhy_controls if c.id == QHYControlId.CONTROL_GAIN), None
        )
        if (
            control is not None
            and control.range is not None
            and settings.gain is not None
            and not (control.range.min <= settings.gain <= control.range.max)
        ):
            self.error(
                f"Gain setting {settings.gain} out of range {control.range.min}..{control.range.max}"
            )
            return

        if (
            self.sdk_call(qhy.CancelQHYCCDExposingAndReadout) != QHYCCD_SUCCESS
        ):  # cancel any ongoing exposure
            return

        self.start_activity(QHYActivities.ExposingSingleFrame)
        self.start_activity(QHYActivities.SettingParameters)

        read_mode = QHYReadMode.Photographic_DSO_16BIT
        if self.sdk_call(qhy.SetQHYCCDReadMode, read_mode) != QHYCCD_SUCCESS:
            return

        stream_mode = QHYStreamMode.SingleFrame
        if self.sdk_call(qhy.SetQHYCCDStreamMode, stream_mode) != QHYCCD_SUCCESS:
            return

        if self.sdk_call(qhy.SetQHYCCDBitsMode, self.bits_mode) != QHYCCD_SUCCESS:
            return

        self.latest_settings = settings
        self.sdk_set_control(
            QHYControlId.CONTROL_EXPOSURE,
            ctypes.c_double(settings.exposure_duration * 1e6),
        )

        if settings.gain is not None:
            if not self.sdk_set_control(
                QHYControlId.CONTROL_GAIN, ctypes.c_double(settings.gain)
            ):
                self.end_activity(QHYActivities.SettingParameters)
                self.end_activity(QHYActivities.ExposingSingleFrame)
                return

        if settings.depth in (8, 16):
            if not self.sdk_set_control(
                QHYControlId.CONTROL_TRANSFERBIT, ctypes.c_double(settings.depth)
            ):
                self.end_activity(QHYActivities.SettingParameters)
                self.end_activity(QHYActivities.ExposingSingleFrame)
                return

        if (
            self.sdk_call(qhy.SetQHYCCDBinMode, settings.binning.x, settings.binning.y)
            != QHYCCD_SUCCESS
        ):
            self.end_activity(QHYActivities.SettingParameters)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        binning = settings.binning.x  # assuming x and y are the same
        roi = settings.roi or QHYRoiModel(
            x=0, y=0, width=self.width.value, height=self.height.value
        )
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

        if settings.gain is not None:
            if not self.sdk_set_control(
                QHYControlId.CONTROL_GAIN, ctypes.c_double(settings.gain)
            ):
                self.end_activity(QHYActivities.SettingParameters)
                self.end_activity(QHYActivities.ExposingSingleFrame)
                return

        self.end_activity(QHYActivities.SettingParameters)

        # Start exposure
        self.info(f"Starting exposure: {settings.exposure_duration:.2f}s")
        if self.sdk_call(qhy.ExpQHYCCDSingleFrame) != QHYCCD_SUCCESS:
            return

        completer = threading.Thread(
            name="qhy600-exposure-completer", target=self.complete_exposure
        )
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
        self._img_buffer = (
            ctypes.c_uint8 if settings.depth == 8 else ctypes.c_uint16 * npixels
        )()

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
            return

        self.end_activity(QHYActivities.ReadingOut)
        self.info(
            f"Image acquired: {width.value}x{height.value}, {bpp.value} bpp, {channels.value} channels"
        )

        if (
            self.latest_settings is not None
            and self.latest_settings.image_path is not None
        ):
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

            hdu.header["EXPTIME"] = settings.exposure_duration
            if settings.gain is not None:
                hdu.header["GAIN"] = settings.gain
            if settings.roi is not None:
                hdu.header["ROI_X"] = settings.roi.x
                hdu.header["ROI_Y"] = settings.roi.y
                hdu.header["ROI_W"] = settings.roi.width
                hdu.header["ROI_H"] = settings.roi.height
            if settings.binning is not None:
                hdu.header["XBINNING"] = settings.binning.x
                hdu.header["YBINNING"] = settings.binning.y
            hdu.header["INSTRUME"] = self.model or "QHY600MM"
            hdu.header["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            # hdu.header["FOCUSPOS"] = 0  # Placeholder for focus position
            hdu.writeto(self.latest_settings.image_path, overwrite=True)
            self.info(
                f"{self.model}: Image saved to {str(self.latest_settings.image_path)}"
            )
            self.end_activity(QHYActivities.Saving)

        self.end_activity(QHYActivities.ReadingOut)
        self.end_activity(QHYActivities.ExposingSingleFrame)

        if self.parent_spec is not None and self.parent_spec.is_active(
            HighspecActivities.Exposing
        ):
            self.parent_spec.end_activity(HighspecActivities.Exposing)

    def startup(self):
        return super().startup()

    def shutdown(self):
        return super().shutdown()

    def status(self):
        return super().status()

    @property
    def operational(self) -> bool:
        return self.detected and self.connected

    def abort(self):
        return super().abort()

    def name(self) -> str:
        return "qhy600mm"

    @property
    def detected(self) -> bool:
        return self.connected

    @property
    def why_not_operational(self) -> list[str]:
        ret: list[str] = []
        if not self.detected:
            ret.append("Camera not detected")
        if not self.connected:
            ret.append("Camera not connected")
        return ret

    @property
    def was_shut_down(self) -> bool:
        return False

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
            image_path=spec_exposure_settings.image_path,
            depth=16,
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
