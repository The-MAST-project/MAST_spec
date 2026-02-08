import ctypes
import logging
import os
import sys
import threading
import time
from enum import IntFlag, auto
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.spec import SpecExposureSettings

from .controls import QHYControl, QHYControlId, qhy_controls

qhy = ctypes.CDLL(
    os.path.join(
        os.path.dirname(__file__), "sdk", "2024-12-26-stable", "x64", "qhyccd.dll"
    )
)

QHYCCD_SUCCESS = 0
STR_BUFFER_SIZE = 32
assert qhy is not None, "Failed to load QHY SDK"

qhy.OpenQHYCCD.argtypes = [
    ctypes.c_char_p,  # id
]
qhy.OpenQHYCCD.restype = ctypes.c_void_p  # handle

qhy.CloseQHYCCD.argtypes = [
    ctypes.c_void_p,  # handle
]
qhy.CloseQHYCCD.restype = ctypes.c_int

qhy.SetQHYCCDResolution.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_uint32,  # x0
    ctypes.c_uint32,  # y0
    ctypes.c_uint32,  # xsize
    ctypes.c_uint32,  # ysize
]
qhy.SetQHYCCDResolution.restype = ctypes.c_int

qhy.GetQHYCCDChipInfo.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.POINTER(ctypes.c_double),  # chipw
    ctypes.POINTER(ctypes.c_double),  # chiph
    ctypes.POINTER(ctypes.c_uint32),  # width
    ctypes.POINTER(ctypes.c_uint32),  # height
    ctypes.POINTER(ctypes.c_double),  # pixelw
    ctypes.POINTER(ctypes.c_double),  # pixelh
    ctypes.POINTER(ctypes.c_uint32),  # bpp
]
qhy.GetQHYCCDChipInfo.restype = ctypes.c_int

qhy.SetQHYCCDBitsMode.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_uint32,  # bits
]
qhy.SetQHYCCDBitsMode.restype = ctypes.c_int

qhy.SetQHYCCDStreamMode.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_uint8,  # mode
]
qhy.SetQHYCCDStreamMode.restype = ctypes.c_int

qhy.GetQHYCCDSDKVersion.argtypes = [
    ctypes.POINTER(ctypes.c_uint32),  # year
    ctypes.POINTER(ctypes.c_uint32),  # month
    ctypes.POINTER(ctypes.c_uint32),  # day
    ctypes.POINTER(ctypes.c_uint32),  # reserved
]
qhy.GetQHYCCDSDKVersion.restype = ctypes.c_int

qhy.GetQHYCCDId.argtypes = [
    ctypes.c_uint32,  # index
    ctypes.c_char_p,  # id (dest buffer)
]
qhy.GetQHYCCDId.restype = ctypes.c_int

qhy.IsQHYCCDControlAvailable.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_int,  # control ID
]
qhy.IsQHYCCDControlAvailable.restype = ctypes.c_int

qhy.GetQHYCCDParam.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_int,  # control ID
]
qhy.GetQHYCCDParam.restype = ctypes.c_double

qhy.SetQHYCCDParam.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_int,  # control ID
    ctypes.c_double,  # value
]
qhy.SetQHYCCDParam.restype = ctypes.c_int

qhy.ExpQHYCCDSingleFrame.argtypes = [
    ctypes.c_void_p,  # handle
]
qhy.ExpQHYCCDSingleFrame.restype = ctypes.c_int

qhy.SetQHYCCDBinMode.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.c_uint32,  # binX
    ctypes.c_uint32,  # binY
]
qhy.SetQHYCCDBinMode.restype = ctypes.c_int

qhy.GetQHYCCDSingleFrame.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.POINTER(ctypes.c_uint32),  # w
    ctypes.POINTER(ctypes.c_uint32),  # h
    ctypes.POINTER(ctypes.c_uint32),  # bpp
    ctypes.POINTER(ctypes.c_uint32),  # channels
    ctypes.c_void_p,  # imgdata (dest buffer)
]
qhy.GetQHYCCDSingleFrame.restype = ctypes.c_uint32

qhy.GetReadModesNumber.argtypes = [
    ctypes.c_void_p,  # handle
    ctypes.POINTER(ctypes.c_uint32),  # num
]

qhy.GetQHYCCDMemLength.argtypes = [
    ctypes.c_void_p,
]
qhy.GetQHYCCDMemLength.restype = ctypes.c_uint32

logger = logging.getLogger(f"mast.highspec.{__name__}")
init_log(logger)


class QHYRoiModel(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


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

        qhy.ReleaseQHYCCDResource()  # type: ignore
        qhy.InitQHYCCDResource()  # type: ignore
        logger.info(f"running in '{os.path.realpath(os.curdir)}'")

        self.cam_id = None
        self.serial_number = None
        self.handle: ctypes.c_ulong | None = None
        # self.handle = None
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
        self.fpgaversion = ctypes.create_string_buffer(32)

        self.stop_event = threading.Event()
        self.read_modes: list[str] = []
        self.latest_settings: QHYCameraSettingsModel | None = None
        self._img_buffer = None

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
            if func.__name__ != "GetQHYCCDMemLength" and ret != QHYCCD_SUCCESS:
                self.error(
                    f"SDK function '{signature}' failed with error code {hex(ret)}"
                )
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

    def initialize_camera(self):
        assert qhy is not None, "Failed to load QHY SDK"
        if qhy.ScanQHYCCD() == 0:
            self.error("No QHY cameras found.")
            return

        self.cam_id = ctypes.create_string_buffer(64)
        assert qhy.GetQHYCCDId(0, self.cam_id) == 0
        s = self.cam_id.value.decode("utf-8")
        self.model, _, self.serial_number = s.partition("-")

        self.handle = qhy.OpenQHYCCD(self.cam_id)

        # nmodes =ctypes.c_uint32(0)
        # if (
        #     ret := self.sdk_call(qhy.GetReadModesNumber, ctypes.byref(nmodes))
        # ) == QHYCCD_SUCCESS:
        #     for read_mode_item_index in range(nmodes.value):
        #         read_mode_name = create_string_buffer(cam.STR_BUFFER_SIZE)
        #         self.sdk_call(
        #             qhy.GetReadModeName,
        #             self.cam_id,
        #             read_mode_item_index,
        #             read_mode_name,
        #         )
        #         self.read_modes.append(read_mode_name.value.decode("utf-8"))
        #         time.sleep(0.1)  # slight delay to avoid overwhelming the camera
        #     logger.debug(f"supports {nmodes.value} read modes")

        if (
            # ret := self.sdk_call(
            #     qhy.GetQHYCCDChipInfo,
            #     ctypes.byref(self.chip_width),
            #     ctypes.byref(self.chip_height),
            #     ctypes.byref(self.width),
            #     ctypes.byref(self.height),
            #     ctypes.byref(self.pixel_width),
            #     ctypes.byref(self.pixel_height),
            #     ctypes.byref(self.bits_per_pixel),
            # )
            ret := qhy.GetQHYCCDChipInfo(
                self.handle,
                ctypes.byref(self.chip_width),
                ctypes.byref(self.chip_height),
                ctypes.byref(self.width),
                ctypes.byref(self.height),
                ctypes.byref(self.pixel_width),
                ctypes.byref(self.pixel_height),
                ctypes.byref(self.bits_per_pixel),
            )
            == QHYCCD_SUCCESS
        ):
            self.debug(
                f"chip info: {self.chip_width.value}mm x {self.chip_height.value}mm, "
                f"{self.width.value} x {self.height.value} pixels, {self.pixel_width.value}um x {self.pixel_height.value}um pixels, "
                f"{self.bits_per_pixel.value} bits per pixel"
            )
        else:
            self.warning(f"Failed to get chip info {ret=}")

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
        if qhy is None or self.handle is None:
            self.error("Camera not connected.")
            return

        self.start_activity(QHYActivities.ExposingSingleFrame)
        self.start_activity(QHYActivities.SettingParameters)

        self.latest_settings = settings
        self.sdk_set_control(
            QHYControlId.CONTROL_EXPOSURE,
            ctypes.c_double(settings.exposure_duration * 1e6),
        )

        # if settings.gain is not None:
        #     self.sdk_set_control(QHYControlId.CONTROL_GAIN,ctypes.c_double(settings.gain))

        # if (
        #     settings.binning is not None
        #     and settings.binning.x == settings.binning.y
        #     and settings.binning.x in (1, 2, 3, 4)
        # ):
        # if settings.binning.x == 1:
        #     binning_control = qhy_controls[QHYControlId.CAM_BIN1X1MODE.value]
        # elif settings.binning.x == 2:
        #     binning_control = qhy_controls[QHYControlId.CAM_BIN2X2MODE.value]
        # elif settings.binning.x == 3:
        #     binning_control = qhy_controls[QHYControlId.CAM_BIN3X3MODE.value]
        # elif settings.binning.x == 4:
        #     binning_control = qhy_controls[QHYControlId.CAM_BIN4X4MODE.value]
        # else:
        #     binning_control = None
        #     self.warning(
        #         f"{self.model}: Binning {settings.binning.x}x{settings.binning.y} not directly supported."
        #     )

        # if binning_control is not None:
        #     if (
        #         ret := self.sdk_call(
        #             qhy.IsQHYCCDControlAvailable, binning_control.id
        #         )
        #     ) == QHYCCD_SUCCESS:
        #         self.sdk_call(
        #             qhy.SetQHYCCDBinMode,
        #            ctypes.c_uint32(settings.binning.x),
        #            ctypes.c_uint32(settings.binning.y),
        #         )
        #     else:
        #         self.warning(
        #             f"Binning control {binning_control.name} not available on this camera."
        #         )

        roi = settings.roi or QHYRoiModel(
            x=0, y=0, width=self.width.value, height=self.height.value
        )
        if (
            ret := self.sdk_call(
                qhy.SetQHYCCDResolution,
                # ctypes.c_uint32(roi.x * settings.binning.x),
                # ctypes.c_uint32(roi.y * settings.binning.y),
                # ctypes.c_uint32(int(roi.xsize / settings.binning.x)),
                # ctypes.c_uint32(int(roi.ysize / settings.binning.y)),
                ctypes.c_uint32(roi.x),
                ctypes.c_uint32(roi.y),
                ctypes.c_uint32(int(roi.width)),
                ctypes.c_uint32(int(roi.height)),
            )
        ) != QHYCCD_SUCCESS:
            self.warning(f"Failed to set ROI: error code {ret}")

        self.sdk_set_control(QHYControlId.CONTROL_USBTRAFFIC, ctypes.c_double(50))

        self.sdk_set_control(
            QHYControlId.CONTROL_TRANSFERBIT, ctypes.c_double(settings.depth)
        )

        # if (
        #     ret := self.sdk_call(qhy.SetQHYCCDBitsMode, settings.depth)
        # ) != QHYCCD_SUCCESS:
        #     self.error(f"Failed to set bits mode to {settings.depth}: error code {ret}")

        # if (
        #     ret := self.sdk_call(qhy.SetQHYCCDStreamMode,ctypes.c_uint8(0))
        # ) != QHYCCD_SUCCESS:
        #     self.error(f"Failed to set stream mode to single frame: error code {ret}")

        self.end_activity(QHYActivities.SettingParameters)

        # # Start exposure
        # self.start_activity(QHYActivities.ExposingAndReadingOut)
        # self.info(f"Starting exposure: {settings.exposure_duration}s")
        # if (ret := self.sdk_call(qhy.ExpQHYCCDSingleFrame)) != QHYCCD_SUCCESS:
        #     self.error(f"Failed to start exposure: error code {ret=}")
        #     return

        # threading.Thread(
        #     name="qhy600-complete-exposure", target=self.complete_exposure
        # ).start()
        # time.sleep(2)
        self.complete_exposure(settings)

    def complete_exposure(self, settings):
        if qhy is None or self.handle is None:
            return

        # Start exposure
        self.start_activity(QHYActivities.ExposingAndReadingOut)
        self.info(f"Starting exposure: {settings.exposure_duration}s")
        if (ret := self.sdk_call(qhy.ExpQHYCCDSingleFrame)) != QHYCCD_SUCCESS:
            self.error(f"Failed to start exposure: error code {ret=}")
            return

        if not self.is_active(QHYActivities.ExposingAndReadingOut):
            self.warning("No exposure in progress to complete.")
            return

        # ret = self.sdk_call(qhy.GetQHYCCDMemLength)
        # ret = qhy.GetQHYCCDMemLength(self.handle)
        # mem_len = int(ret) if ret is not None else 0
        # if not mem_len > 0:
        #     self.error("Invalid memory length retrieved from camera.")
        #     self.end_activity(QHYActivities.ExposingAndReadingOut)
        #     self.end_activity(QHYActivities.ExposingSingleFrame)
        #     return
        # self.debug(f"SDK reports required buffer size: {mem_len} bytes")
        # self.debug(
        #     f"For comparison: width*height*bytes_per_pixel = {self.width.value * self.height.value * (self.bits_per_pixel.value // 8)}"
        # )

        # self.info(f"{mem_len=}, {mem_len / (self.width.value * self.height.value)}")

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
                width = settings.roi.width
                height = settings.roi.height

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

        # if bits_per_pixel == 8:
        #     self._img_buffer = (ctypes.c_uint8 * width * height)()
        # elif bits_per_pixel == 16:
        #     self._img_buffer = (ctypes.c_uint8 * width * height * 2)()

        # self._img_buffer = (ctypes.c_uint8 * mem_len)()
        self._img_buffer_type = ctypes.c_uint8 * nbytes
        self._img_buffer = self._img_buffer_type()
        self._buffer_ref = self._img_buffer
        buffer_p = ctypes.cast(self._img_buffer, ctypes.POINTER(ctypes.c_uint8))
        self._buffer_p_ref = buffer_p

        self.debug(f"Buffer at: {hex(ctypes.addressof(self._img_buffer))}")
        self.debug(
            f"Buffer pointer at: {hex(ctypes.cast(buffer_p, ctypes.c_void_p).value)}"
        )

        # lib = ctypes.CDLL(
        #     os.path.join(os.path.dirname(__file__), "dummyqhy.dll")
        # )  # Use CDLL (cdecl)

        # assert self._img_buffer
        # lib.DummyBufferAddress.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        # lib.DummyBufferAddress.restype = ctypes.c_size_t  # uintptr_t
        # addr_from_c = lib.DummyBufferAddress(
        #     ctypes.cast(self._img_buffer, ctypes.POINTER(ctypes.c_ubyte))
        # )
        # print("C sees image_buffer address     :", hex(addr_from_c))

        width = ctypes.c_uint32()
        height = ctypes.c_uint32()
        bpp = ctypes.c_uint32()
        channels = ctypes.c_uint32()
        # self.debug(
        #     f"self.handle={self.handle}, width={width}, height={height}, bpp={bpp}, channels={channels}, image_buffer={img_buffer}"
        # )
        # try:
        ret = qhy.GetQHYCCDSingleFrame(
            self.handle,
            ctypes.byref(width),
            ctypes.byref(height),
            ctypes.byref(bpp),
            ctypes.byref(channels),
            ctypes.cast(self._img_buffer, ctypes.c_void_p),
        )
        # except Exception as ex:
        #     self.error(f"{ex=}")
        #     return

        # try:
        #     if (
        #         ret := self.sdk_call(
        #             qhy.GetQHYCCDSingleFrame,  # blocking call
        #             ctypes.byref(width),
        #             ctypes.byref(height),
        #             ctypes.byref(bpp),
        #             ctypes.byref(channels),
        #             # cast(self._img_buffer,ctypes.POINTER(ctypes.c_ubyte)),
        #             self._img_buffer,
        #         )
        #     ) != QHYCCD_SUCCESS:
        #         self.error(f"Failed to read out image: error code {ret=}")
        #         self.end_activity(QHYActivities.ExposingAndReadingOut)
        #         self.end_activity(QHYActivities.ExposingSingleFrame)
        #         self.debug(f"{width=}, {height=}, {bpp=}, {channels=}")
        #         return
        # except Exception as e:
        #     self.error(f"Exception during image readout: {e=}")
        #     self.end_activity(QHYActivities.ExposingAndReadingOut)
        #     self.end_activity(QHYActivities.ExposingSingleFrame)
        #     return

        # Convert the image data to a more usable format, e.g., a NumPy array
        import numpy as np

        img_array = np.ctypeslib.as_array(self._img_buffer)
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
        if qhy is None or self.handle is None:
            return

        if self.is_active(QHYActivities.ExposingSingleFrame):
            qhy.CancelQHYCCDExposingAndReadout(self.handle)
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
        import os

        lib = ctypes.CDLL(
            os.path.join(os.path.dirname(__file__), "dummyqhy.dll")
        )  # Use CDLL (cdecl)

        # Declare signatures
        lib.DummyGetQHYCCDSingleFrame.argtypes = [
            ctypes.c_void_p,  # handle
            ctypes.POINTER(ctypes.c_uint32),  # w
            ctypes.POINTER(ctypes.c_uint32),  # h
            ctypes.POINTER(ctypes.c_uint32),  # bpp
            ctypes.POINTER(ctypes.c_uint32),  # ch
            ctypes.POINTER(ctypes.c_ubyte),  # imgdata
        ]
        lib.DummyGetQHYCCDSingleFrame.restype = ctypes.c_uint32

        lib.DummyBufferAddress.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        lib.DummyBufferAddress.restype = ctypes.c_size_t  # uintptr_t

        # Allocate buffer exactly like you do for QHY
        nbytes = 32
        image_buffer = (
            ctypes.c_uint8 * nbytes
        )()  # <-- array object; decays to uint8_t*
        buf_addr_py = ctypes.addressof(image_buffer)
        print("Python sees image_buffer address:", hex(buf_addr_py))

        # Optional: ask C to print & return the pointer it sees
        addr_from_c = lib.DummyBufferAddress(image_buffer)
        print("C sees image_buffer address     :", hex(addr_from_c))

        # Prepare out-params
        w = ctypes.c_uint32(0)
        h = ctypes.c_uint32(0)
        bpp = ctypes.c_uint32(0)
        ch = ctypes.c_uint32(0)

        # Call the dummy "Get" function
        ret = lib.DummyGetQHYCCDSingleFrame(
            None,
            ctypes.byref(w),
            ctypes.byref(h),
            ctypes.byref(bpp),
            ctypes.byref(ch),
            image_buffer,  # IMPORTANT: pass the array, not ctypes.byref(array)
        )
        print("ret =", ret, "w,h,bpp,ch =", w.value, h.value, bpp.value, ch.value)

        # Show the pattern written by C (0..31)
        print(list(image_buffer[:32]))

    test_single_exposure()
    # test_dummy_qhy()
    camera.disconnect()
    sys.exit(0)
