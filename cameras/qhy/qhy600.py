import logging
import os
import sys
import threading
import time
from ctypes import (
    CDLL,
    POINTER,
    addressof,
    byref,
    c_char_p,
    c_double,
    c_int,
    c_ubyte,
    c_uint8,
    c_uint32,
    c_void_p,
    cast,
    create_string_buffer,
)
from enum import IntFlag, auto
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

# from qcam.qCam import Qcam
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.spec import SpecExposureSettings

from .controls import QHYControlId, qhy_controls


class MyQcam:
    QHYCCD_SUCCESS = 0
    STR_BUFFER_SIZE = 32

    def __init__(self, dll_path):
        self.so = CDLL(dll_path)


cam = MyQcam(
    os.path.join(
        os.path.dirname(__file__), "sdk", "2024-12-26-stable", "x64", "qhyccd.dll"
    )
)
assert cam.so is not None, "Failed to load QHY SDK"

cam.so.OpenQHYCCD.argtypes = [
    c_char_p,  # id
]
cam.so.OpenQHYCCD.restype = c_void_p  # handle

cam.so.CloseQHYCCD.argtypes = [
    c_void_p,  # handle
]
cam.so.CloseQHYCCD.restype = c_int

cam.so.SetQHYCCDResolution.argtypes = [
    c_void_p,  # handle
    c_uint32,  # x0
    c_uint32,  # y0
    c_uint32,  # xsize
    c_uint32,  # ysize
]
cam.so.SetQHYCCDResolution.restype = c_int

cam.so.GetQHYCCDChipInfo.argtypes = [
    c_void_p,  # handle
    POINTER(c_double),  # chipw
    POINTER(c_double),  # chiph
    POINTER(c_uint32),  # width
    POINTER(c_uint32),  # height
    POINTER(c_double),  # pixelw
    POINTER(c_double),  # pixelh
    POINTER(c_uint32),  # bpp
]
cam.so.GetQHYCCDChipInfo.restype = c_int

cam.so.SetQHYCCDBitsMode.argtypes = [
    c_void_p,  # handle
    c_uint32,  # bits
]
cam.so.SetQHYCCDBitsMode.restype = c_int

cam.so.SetQHYCCDStreamMode.argtypes = [
    c_void_p,  # handle
    c_uint8,  # mode
]
cam.so.SetQHYCCDStreamMode.restype = c_int

cam.so.GetQHYCCDSDKVersion.argtypes = [
    POINTER(c_uint32),  # year
    POINTER(c_uint32),  # month
    POINTER(c_uint32),  # day
    POINTER(c_uint32),  # reserved
]
cam.so.GetQHYCCDSDKVersion.restype = c_int

cam.so.GetQHYCCDId.argtypes = [
    c_uint32,  # index
    c_char_p,  # id (dest buffer)
]
cam.so.GetQHYCCDId.restype = c_int

cam.so.IsQHYCCDControlAvailable.argtypes = [
    c_void_p,  # handle
    c_int,  # control ID
]
cam.so.IsQHYCCDControlAvailable.restype = c_int

cam.so.GetQHYCCDParam.argtypes = [
    c_void_p,  # handle
    c_int,  # control ID
]
cam.so.GetQHYCCDParam.restype = c_double

cam.so.SetQHYCCDParam.argtypes = [
    c_void_p,  # handle
    c_int,  # control ID
    c_double,  # value
]
cam.so.SetQHYCCDParam.restype = c_int

cam.so.ExpQHYCCDSingleFrame.argtypes = [
    c_void_p,  # handle
]
cam.so.ExpQHYCCDSingleFrame.restype = c_int

cam.so.SetQHYCCDBinMode.argtypes = [
    c_void_p,  # handle
    c_uint32,  # binX
    c_uint32,  # binY
]
cam.so.SetQHYCCDBinMode.restype = c_int

cam.so.GetQHYCCDSingleFrame.argtypes = [
    c_void_p,  # handle
    POINTER(c_uint32),  # w
    POINTER(c_uint32),  # h
    POINTER(c_uint32),  # bpp
    POINTER(c_uint32),  # channels
    POINTER(c_uint8),  # imgdata (dest buffer)
]
cam.so.GetQHYCCDSingleFrame.restype = c_uint32

cam.so.GetReadModesNumber.argtypes = [
    c_void_p,  # handle
    POINTER(c_uint32),  # num
]

logger = logging.getLogger(f"mast.highspec.{__name__}")
init_log(logger)


class QHYRoiModel(BaseModel):
    x: int = 0
    y: int = 0
    xsize: int = 0
    ysize: int = 0


class QHYBinningModel(BaseModel):
    x: int = 1
    y: int = 1


class QHYCameraSettingsModel(BaseModel):
    binning: QHYBinningModel = QHYBinningModel(x=1, y=1)
    roi: QHYRoiModel | None = None
    # temperature: Optional[NewtonTemperatureModel]
    # shutter: Optional[NewtonShutterModel]
    gain: int | None = None
    exposure_duration: float = 1.0  # in seconds
    number_of_exposures: int = 1
    image_path: str | Path | None = None  # full path to save image
    depth: Literal[8, 16] = 16  # bits per pixel


class QHYActivities(IntFlag):
    Idle = auto()
    Acquiring = auto()
    ExposingSingleFrame = auto()
    SettingParameters = auto()
    ExposingAndReadingOut = auto()
    Saving = auto()


class QHYSettingsModel(BaseModel):
    exposure: float = 1.0  # in seconds
    gain: int = 0
    offset: int = 0
    readout_speed: int = 0  # index into readout speeds
    hsspeed: int = 0  # index into horizontal shift speeds
    preamp_gain: int = 0  # index into preamp gains
    cooling_temperature: float = -10.0  # in Celsius
    cooler_on: bool = True
    high_speed_mode: bool = False
    trigger_mode: int = 0  # 0=internal, 1=external, etc.
    image_flip: bool = False
    binning: QHYBinningModel = QHYBinningModel()
    save_directory: Path = Path("C:/Images")
    file_format: str = "FITS"  # or "TIFF", "JPEG", etc.


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

        Component.__init__(self)
        # SwitchedOutlet.__init__(
        #     self, domain=OutletDomain.SpecOutlets, outlet_name="QHY600U3"
        # )

        cam.so.ReleaseQHYCCDResource()  # type: ignore
        cam.so.InitQHYCCDResource()  # type: ignore
        logger.info(f"running in '{os.path.realpath(os.curdir)}'")

        self.cam_id = None
        self.serial_number = None
        self.handle = None
        self.chip_width = c_double()
        self.chip_height = c_double()
        self.width = c_uint32()
        self.height = c_uint32()
        self.pixel_width = c_double()
        self.pixel_height = c_double()
        self.channels = c_uint32()
        self.bits_per_pixel = c_uint32()
        self.bits_mode = 16
        self.fw_version = create_string_buffer(32)
        self.fpgaversion = create_string_buffer(32)

        self.stop_event = threading.Event()
        self.read_modes: list[str] = []
        self.latest_settings: QHYCameraSettingsModel | None = None
        self.image_buffer = None

        self._connected = False
        self.connect()
        self._initialized = True

    def sdk_call(self, func: Callable, *args):
        if not self.connected:
            self.error("Camera not connected.")
            return None

        signature = f"{func.__name__}({[f'{arg}' for arg in args]})".replace(
            "[", ""
        ).replace("]", "")
        try:
            ret = func(self.handle, *args)
            if func.__name__ != "GetQHYCCDMemLength" and ret != cam.QHYCCD_SUCCESS:
                self.error(f"SDK function '{signature}' failed with error code {ret}")
                return None
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
            assert cam.so is not None, "QHY SDK not loaded"
            value = cam.so.GetQHYCCDParam(self.handle, control_id.value)
            self.debug(f"SDK get control {control_id.name} returned {value}")
            return value
        except Exception as e:
            self.error(f"Error getting control {control_id}: {e}")
            return None

    def sdk_set_control(self, control_id: QHYControlId, value: float) -> bool:
        if not self.connected:
            self.error("Camera not connected.")
            return False
        try:
            assert cam.so is not None, "QHY SDK not loaded"
            found = [ctrl for ctrl in qhy_controls if ctrl.id == control_id]
            if not found:
                self.error(f"Control ID {control_id} not recognized.")
                return False
            control = found[0]

            if control.range is not None:
                min_val, max_val = control.range.min, control.range.max
                if not (min_val <= value <= max_val):
                    self.error(
                        f"Value {value} for control '{control_id.name}' out of range ({min_val}, {max_val})"
                    )
                    return False
            if (
                ret := cam.so.SetQHYCCDParam(self.handle, control_id.value, value)
            ) != cam.QHYCCD_SUCCESS:
                self.error(
                    f"Failed to set control {control_id} to {value}: error code {ret}"
                )
                return False
            self.debug(f"SDK set control {control_id.name} to {value}")
            return True
        except Exception as e:
            self.error(f"Error setting control {control_id} to {value}: {e=}")
            return False

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
        if cam.so is not None:
            self.info("Disconnecting")
            if cam.so and self.handle:
                cam.so.CloseQHYCCD(self.handle)
            cam.so.ReleaseQHYCCDResource()
        self.handle = None
        self.cam_id = None
        self.model = None
        self.serial_number = None
        self._connected = False

    def initialize_camera(self):
        assert cam.so is not None, "Failed to load QHY SDK"
        if cam.so.ScanQHYCCD() == 0:
            self.error("No QHY cameras found.")
            return

        self.cam_id = create_string_buffer(64)
        assert cam.so.GetQHYCCDId(0, self.cam_id) == 0
        s = self.cam_id.value.decode("utf-8")
        self.model, _, self.serial_number = s.partition("-")

        self.handle = cam.so.OpenQHYCCD(self.cam_id)

        # nmodes = c_uint32(0)
        # if (
        #     ret := self.sdk_call(cam.so.GetReadModesNumber, byref(nmodes))
        # ) == cam.QHYCCD_SUCCESS:
        #     for read_mode_item_index in range(nmodes.value):
        #         read_mode_name = create_string_buffer(cam.STR_BUFFER_SIZE)
        #         self.sdk_call(
        #             cam.so.GetReadModeName,
        #             self.cam_id,
        #             read_mode_item_index,
        #             read_mode_name,
        #         )
        #         self.read_modes.append(read_mode_name.value.decode("utf-8"))
        #         time.sleep(0.1)  # slight delay to avoid overwhelming the camera
        #     logger.debug(f"supports {nmodes.value} read modes")

        if (
            # ret := self.sdk_call(
            #     cam.so.GetQHYCCDChipInfo,
            #     byref(self.chip_width),
            #     byref(self.chip_height),
            #     byref(self.width),
            #     byref(self.height),
            #     byref(self.pixel_width),
            #     byref(self.pixel_height),
            #     byref(self.bits_per_pixel),
            # )
            ret := cam.so.GetQHYCCDChipInfo(
                self.handle,
                byref(self.chip_width),
                byref(self.chip_height),
                byref(self.width),
                byref(self.height),
                byref(self.pixel_width),
                byref(self.pixel_height),
                byref(self.bits_per_pixel),
            )
            == cam.QHYCCD_SUCCESS
        ):
            self.debug(
                f"chip info: {self.chip_width.value}mm x {self.chip_height.value}mm, "
                f"{self.width.value} x {self.height.value} pixels, {self.pixel_width.value}um x {self.pixel_height.value}um pixels, "
                f"{self.bits_per_pixel.value} bits per pixel"
            )
        else:
            self.warning(f"Failed to get chip info {ret=}")

        year = c_uint32()
        month = c_uint32()
        day = c_uint32()
        zero = c_uint32(0)

        if (
            ret := cam.so.GetQHYCCDSDKVersion(
                byref(year),
                byref(month),
                byref(day),
                byref(zero),
            )
        ) == cam.QHYCCD_SUCCESS:
            self.debug(
                f"SDK version: year=20{year.value} month={month.value:02} day={day.value:02}"
            )
        else:
            self.warning(f"Failed to get SDK version {ret=}")

    def status(self):
        return {
            "activities": self.activities,
            "activities_verbose": self.activities.__repr__(),
            "connected": self.connected,
            "model": self.model,
            "serial_number": self.serial_number,
            "width": self.width.value if self.width else None,
            "height": self.height.value if self.height else None,
            "pixel_width_um": self.pixel_width.value if self.pixel_width else None,
            "pixel_height_um": self.pixel_height.value if self.pixel_height else None,
            "bits_per_pixel": self.bits_per_pixel.value
            if self.bits_per_pixel
            else None,
            "latest_settings": self.latest_settings.__dict__
            if self.latest_settings
            else None,
        }

    def abort(self):
        if self.is_active(QHYActivities.Acquiring):
            self.stop_event.set()

    def start_acquisition(self, settings: SpecExposureSettings):
        if self.is_active(QHYActivities.Acquiring):
            self.warning("Acquisition already in progress.")
            return

        camera_settings = QHYCameraSettingsModel(
            binning=QHYBinningModel(x=1, y=1),
            roi=None,
            gain=None,
            exposure_duration=settings.exposure_duration,
            image_path=settings.image_path,
        )

        self.start_activity(QHYActivities.Acquiring)
        for seq in range(settings.number_of_exposures or 1):
            self.start_single_exposure(camera_settings)
            while self.is_active(QHYActivities.ExposingAndReadingOut) or self.is_active(
                QHYActivities.Saving
            ):
                if self.stop_event.is_set():
                    self.info("Acquisition aborted.")
                    self.stop_event.clear()
                    return
                threading.Event().wait(0.1)
        self.end_activity(QHYActivities.Acquiring)

    def start_single_exposure(self, settings: QHYCameraSettingsModel):
        if cam.so is None or self.handle is None:
            self.error("Camera not connected.")
            return

        self.start_activity(QHYActivities.ExposingSingleFrame)
        self.start_activity(QHYActivities.SettingParameters)

        self.latest_settings = settings
        self.sdk_set_control(
            QHYControlId.CONTROL_EXPOSURE, settings.exposure_duration * 1000
        )

        if settings.gain is not None:
            self.sdk_set_control(
                QHYControlId.CONTROL_GAIN, c_double(settings.gain).value
            )

        if (
            settings.binning is not None
            and settings.binning.x == settings.binning.y
            and settings.binning.x in (1, 2, 3, 4)
        ):
            if settings.binning.x == 1:
                binning_control = qhy_controls[QHYControlId.CAM_BIN1X1MODE]
            elif settings.binning.x == 2:
                binning_control = qhy_controls[QHYControlId.CAM_BIN2X2MODE]
            elif settings.binning.x == 3:
                binning_control = qhy_controls[QHYControlId.CAM_BIN3X3MODE]
            elif settings.binning.x == 4:
                binning_control = qhy_controls[QHYControlId.CAM_BIN4X4MODE]
            else:
                binning_control = None
                self.warning(
                    f"{self.model}: Binning {settings.binning.x}x{settings.binning.y} not directly supported."
                )

            if binning_control is not None:
                if (
                    ret := self.sdk_call(
                        cam.so.IsQHYCCDControlAvailable, binning_control.id
                    )
                ) == cam.QHYCCD_SUCCESS:
                    self.sdk_call(
                        cam.so.SetQHYCCDBinMode,
                        c_uint32(settings.binning.x),
                        c_uint32(settings.binning.y),
                    )
                else:
                    self.warning(
                        f"Binning control {binning_control.name} not available on this camera."
                    )

        roi = settings.roi or QHYRoiModel(
            x=0, y=0, xsize=self.width.value, ysize=self.height.value
        )
        if (
            ret := self.sdk_call(
                cam.so.SetQHYCCDResolution,
                # c_uint32(roi.x * settings.binning.x),
                # c_uint32(roi.y * settings.binning.y),
                # c_uint32(int(roi.xsize / settings.binning.x)),
                # c_uint32(int(roi.ysize / settings.binning.y)),
                c_uint32(roi.x),
                c_uint32(roi.y),
                c_uint32(int(roi.xsize)),
                c_uint32(int(roi.ysize)),
            )
        ) != cam.QHYCCD_SUCCESS:
            self.warning(f"Failed to set ROI: error code {ret}")

        if (
            ret := self.sdk_call(cam.so.SetQHYCCDBitsMode, settings.depth)
        ) != cam.QHYCCD_SUCCESS:
            self.error(f"Failed to set bits mode to {settings.depth}: error code {ret}")

        if (
            ret := self.sdk_call(cam.so.SetQHYCCDStreamMode, c_uint8(0))
        ) != cam.QHYCCD_SUCCESS:
            self.error(f"Failed to set stream mode to single frame: error code {ret}")

        self.end_activity(QHYActivities.SettingParameters)

        # Start exposure
        self.start_activity(QHYActivities.ExposingAndReadingOut)
        self.info(f"Starting exposure: {settings.exposure_duration}s")
        if (ret := self.sdk_call(cam.so.ExpQHYCCDSingleFrame)) != cam.QHYCCD_SUCCESS:
            self.error(f"Failed to start exposure: error code {ret=}")
            return

        # threading.Thread(
        #     name="qhy600-complete-exposure", target=self.complete_exposure
        # ).start()
        time.sleep(2)
        self.complete_exposure()

    def complete_exposure(self):
        if cam.so is None or self.handle is None:
            return

        if not self.is_active(QHYActivities.ExposingAndReadingOut):
            self.warning("No exposure in progress to complete.")
            return

        # ret = self.sdk_call(cam.so.GetQHYCCDMemLength)
        # nbytes = int(ret) if ret is not None else 0
        # if not nbytes > 0:
        #     self.error("Invalid memory length retrieved from camera.")
        #     self.end_activity(QHYActivities.ExposingAndReadingOut)
        #     self.end_activity(QHYActivities.ExposingSingleFrame)
        #     return

        # defaults
        width = self.width.value
        height = self.height.value
        bits_per_pixel = self.bits_per_pixel.value
        x_binning = 1
        y_binning = 1

        if self.latest_settings is not None:
            # override defaults from settings
            settings = self.latest_settings
            if settings.roi is not None:
                width = settings.roi.xsize
                height = settings.roi.ysize

            if settings.binning is not None:
                x_binning = settings.binning.x
                y_binning = settings.binning.y

            bits_per_pixel = settings.depth

        self.debug(
            f"Image parameters for readout: {width=} {height=} {x_binning=} {y_binning=} {bits_per_pixel=}"
        )
        nbytes = int(
            (width // x_binning) * (height // y_binning) * (bits_per_pixel // 8)
        )

        image_buffer = (c_uint8 * nbytes)()
        self.debug(
            f"Allocated {nbytes=} image buffer at {hex(addressof(image_buffer))=}"
        )
        image_buffer[0:10] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        import ctypes as C

        lib = C.CDLL(
            os.path.join(os.path.dirname(__file__), "dummyqhy.dll")
        )  # Use CDLL (cdecl)

        lib.DummyBufferAddress.argtypes = [C.POINTER(C.c_ubyte)]
        lib.DummyBufferAddress.restype = C.c_size_t  # uintptr_t
        addr_from_c = lib.DummyBufferAddress(image_buffer)
        print("C sees image_buffer address     :", hex(addr_from_c))

        width = c_uint32(0)
        height = c_uint32(0)
        bpp = c_uint32(0)
        channels = c_uint32(0)
        self.debug(
            f"self.handle={self.handle}, width={width}, height={height}, bpp={bpp}, channels={channels}, image_buffer={image_buffer}"
        )
        try:
            if (
                ret := self.sdk_call(
                    cam.so.GetQHYCCDSingleFrame,  # blocking call
                    byref(width),
                    byref(height),
                    byref(bpp),
                    byref(channels),
                    image_buffer,
                )
            ) != cam.QHYCCD_SUCCESS:
                self.error(f"Failed to read out image: error code {ret=}")
                self.end_activity(QHYActivities.ExposingAndReadingOut)
                self.end_activity(QHYActivities.ExposingSingleFrame)
                self.debug(f"{width=}, {height=}, {bpp=}, {channels=}")
                return
        except Exception as e:
            self.error(f"Exception during image readout: {e=}")
            self.end_activity(QHYActivities.ExposingAndReadingOut)
            self.end_activity(QHYActivities.ExposingSingleFrame)
            return

        # Convert the image data to a more usable format, e.g., a NumPy array
        import numpy as np

        img_array = np.ctypeslib.as_array(image_buffer)
        img_array = img_array.reshape((height.value, width.value))

        self.end_activity(QHYActivities.ExposingAndReadingOut)
        self.info(f"{self.model}: Exposure complete and image read out.")

        if (
            self.latest_settings is not None
            and self.latest_settings.image_path is not None
        ):
            self.start_activity(QHYActivities.Saving)

            from astropy.io import fits

            hdu = fits.PrimaryHDU(img_array)
            hdu.writeto(self.latest_settings.image_path, overwrite=True)
            self.info(
                f"{self.model}: Image saved to {str(self.latest_settings.image_path)}"
            )
            self.end_activity(QHYActivities.Saving)

        self.end_activity(QHYActivities.ExposingSingleFrame)

    def stop_exposure(self):
        if cam.so is None or self.handle is None:
            return

        if self.is_active(QHYActivities.ExposingSingleFrame):
            cam.so.CancelQHYCCDExposingAndReadout(self.handle)
            self.end_activity(QHYActivities.ExposingSingleFrame)

    def startup(self):
        pass

    def shutdown(self):
        self.abort()
        self.connected = False

    def name(self) -> str:
        return self.model if self.model else "QHY600U3"

    @property
    def operational(self) -> bool:
        return self.connected

    @property
    def why_not_operational(self) -> list[str]:
        if not self.connected:
            return [f"{self.model}: not connected."]
        return []

    @property
    def detected(self) -> bool:
        return self.connected

    @property
    def was_shut_down(self) -> bool:
        return False

    def info(self, message):
        logger.info(f"{self.model if hasattr(self, 'model') else 'Unknown'}: {message}")

    def warning(self, message):
        logger.warning(
            f"{self.model if hasattr(self, 'model') else 'Unknown'}: {message}"
        )

    def error(self, message):
        logger.error(
            f"{self.model if hasattr(self, 'model') else 'Unknown'}: {message}"
        )

    def debug(self, message):
        logger.debug(f"{self.model}: {message}")


if __name__ == "__main__":
    camera = QHY600()

    def test_single_exposure():
        camera.start_single_exposure(
            QHYCameraSettingsModel(
                exposure_duration=5.0,
                image_path="c:/qhy_images/test_image.fits",
                depth=16,
                # roi=QHYRoiModel(x=10, y=10, xsize=1000, ysize=1000),
                # binning=QHYBinningModel(x=2, y=2),
            )
        )
        while camera.is_active(QHYActivities.ExposingSingleFrame):
            time.sleep(0.5)

    def test_dummy_qhy():
        import ctypes as C
        import os
        import sys

        lib = C.CDLL(
            os.path.join(os.path.dirname(__file__), "dummyqhy.dll")
        )  # Use CDLL (cdecl)

        # Declare signatures
        lib.DummyGetQHYCCDSingleFrame.argtypes = [
            C.c_void_p,  # handle
            C.POINTER(C.c_uint32),  # w
            C.POINTER(C.c_uint32),  # h
            C.POINTER(C.c_uint32),  # bpp
            C.POINTER(C.c_uint32),  # ch
            C.POINTER(C.c_ubyte),  # imgdata
        ]
        lib.DummyGetQHYCCDSingleFrame.restype = C.c_uint32

        lib.DummyBufferAddress.argtypes = [C.POINTER(C.c_ubyte)]
        lib.DummyBufferAddress.restype = C.c_size_t  # uintptr_t

        # Allocate buffer exactly like you do for QHY
        nbytes = 32
        image_buffer = (C.c_uint8 * nbytes)()  # <-- array object; decays to uint8_t*
        buf_addr_py = C.addressof(image_buffer)
        print("Python sees image_buffer address:", hex(buf_addr_py))

        # Optional: ask C to print & return the pointer it sees
        addr_from_c = lib.DummyBufferAddress(image_buffer)
        print("C sees image_buffer address     :", hex(addr_from_c))

        # Prepare out-params
        w = C.c_uint32(0)
        h = C.c_uint32(0)
        bpp = C.c_uint32(0)
        ch = C.c_uint32(0)

        # Call the dummy "Get" function
        ret = lib.DummyGetQHYCCDSingleFrame(
            None,
            C.byref(w),
            C.byref(h),
            C.byref(bpp),
            C.byref(ch),
            image_buffer,  # IMPORTANT: pass the array, not byref(array)
        )
        print("ret =", ret, "w,h,bpp,ch =", w.value, h.value, bpp.value, ch.value)

        # Show the pattern written by C (0..31)
        print(list(image_buffer[:32]))

    test_single_exposure()
    # test_dummy_qhy()
    camera.disconnect()
    sys.exit(0)
