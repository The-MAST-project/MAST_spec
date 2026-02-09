import datetime
import logging
import os
import sys
import threading
import time
from datetime import timezone
from enum import IntEnum
from typing import Callable, get_args

import astropy.io.fits as fits
from astropy.io.fits import Card
from pydantic import BaseModel

from common.activities import GreatEyesActivities
from common.config import Config
from common.dlipowerswitch import SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import SpectrographAssignmentModel
from common.models.deepspec import DeepspecModel
from common.models.greateyes import GreateyesSettingsModel, ReadoutSpeed
from common.models.statuses import GreateyesStatus
from common.networking import NetworkedDevice
from common.spec import DeepspecBands
from common.utils import OperatingMode, RepeatTimer, function_name

sys.path.append(os.path.join(os.path.dirname(__file__), "sdk"))
import cameras.greateyes.sdk.greateyesSDK as ge

logger = logging.getLogger("greateyes")
init_log(logger, logging.DEBUG)

dll_version = ge.GetDLLVersion()
shown_dll_version = False

if not shown_dll_version:
    logger.info(f"Greateyes DLL version: '{dll_version}'")
    shown_dll_version = True

FAILED_TEMPERATURE = -300

FITS_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# class ReadoutAmplifiers(IntEnum):
#     OSR = 0,
#     OSL = 1,
#     OSR_AND_OSL = 2,


class CropSettingsModel(BaseModel):
    col: int
    line: int
    enabled: bool


class BytesPerPixel(IntEnum):
    Two = 2
    Three = 3
    Four = 4


readout_speed_names = {
    ReadoutSpeed.ReadoutSpeed_1_MHz: "1 MHz",
    ReadoutSpeed.ReadoutSpeed_3_MHz: "3 MHz",
    ReadoutSpeed.ReadoutSpeed_500_kHz: "500 KHz",
    ReadoutSpeed.ReadoutSpeed_250_kHz: "250 KHz",
    ReadoutSpeed.ReadoutSpeed_100_kHz: "100 KHz",
    ReadoutSpeed.ReadoutSpeed_50_kHz: "50 KHz",
}


class ExposureTiming:
    start: datetime.datetime
    start_utc: datetime.datetime

    mid: datetime.datetime
    mid_utc: datetime.datetime

    end: datetime.datetime
    end_utc: datetime.datetime

    duration: datetime.timedelta


class Exposure:
    settings: GreateyesSettingsModel | None = None
    timing: ExposureTiming | None = None

    def __init__(self):
        self.timing = ExposureTiming()
        self.timing.start = datetime.datetime.now()
        self.timing.start_utc = self.timing.start.astimezone(timezone.utc)

    def to_dict(self):
        return {
            "settings": self.settings.model_dump() if self.settings else None,
            "timing": self.timing.__dict__ if self.timing else None,
        }


class GreatEyes(SwitchedOutlet, NetworkedDevice, Component):
    def __init__(self, band: DeepspecBands):
        self._initialized = False
        self._detected = False
        self._connected = False
        Component.__init__(self, GreatEyesActivities)

        self.band = band
        self.conf = (
            Config().get_specs().deepspec[self.band]
        )  # specific to this camera instance
        assert self.conf.settings is not None
        self.settings: GreateyesSettingsModel = GreateyesSettingsModel(
            **self.conf.settings.model_dump()
        )
        self.latest_settings: GreateyesSettingsModel | None = None
        self.ge_device = self.conf.device
        self._name = f"Deepspec-{self.band}"
        self.outlet_name = f"Deepspec{self.band}"
        self.errors = []
        self.output_modes: list[str] = []

        from common.dlipowerswitch import OutletDomain, SwitchedOutlet

        assert self.conf.network is not None
        NetworkedDevice.__init__(self, self.conf.model_dump())
        SwitchedOutlet.__init__(
            self, outlet_name=f"{self.outlet_name}", domain=OutletDomain.SpecOutlets
        )

        self.settings: GreateyesSettingsModel = GreateyesSettingsModel(
            **self.conf.settings.model_dump()
        )

        self.enabled = self.conf.enabled

        self.acquisition: str | None = None

        self.last_backside_temp_check: datetime.datetime | None = None
        self.backside_temp_safe = True

        self.readout_thread: threading.Thread | None = None

        self.latest_exposure = Exposure()

        self.model_id = None
        self.model = None
        self.firmware_version = None

        self.min_temp = None
        self.max_temp = None

        self.x_size = None
        self.y_size = None
        self.bytes_per_pixel = None

        self.pixel_size_microns = None

        self._was_shut_down = False
        self._initialized = True

        if not self.enabled:
            self.warning(f"camera {self._name} is disabled")
            return

        self.shutdown_event: threading.Event = threading.Event()

        self.last_probe_time = None
        self.timer = RepeatTimer(1, function=self.on_timer)
        self.timer.name = f"deepspec-camera-{self.band}-timer-thread"
        self.timer.start()

    def try_connect_camera(self):
        #
        # Clean-up previous connections, if existent
        # NOTE: these actions may return False, but that seems OK
        #
        assert self.ge_device is not None
        ret = ge.DisconnectCamera(addr=self.ge_device)
        self.debug(f"ge.DisconnectCamera(addr={self.ge_device}) -> {ret}")
        try:
            ret = ge.DisconnectCameraServer(addr=self.ge_device)
            self.debug(f"ge.DisconnectCameraServer(addr={self.ge_device}) -> {ret}")
        except Exception as e:
            self.error(
                f"ge.DisconnectCameraServer(addr={self.ge_device}) caught error {e}, ignoring."
            )
            # return

        # This just tells the Greateyes server how to interface with the specific camera
        # NOTE: it should not fail
        ret = ge.SetupCameraInterface(
            ge.connectionType_Ethernet,
            ipAddress=self.network.ipaddr,
            addr=self.ge_device,
        )
        if not ret:
            self.error(
                f"could not ge.SetupCameraInterface({ge.connectionType_Ethernet}, "
                + f"ipaddress={self.network.ipaddr}, addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')"
            )
            self.end_activity(GreatEyesActivities.Probing, label=self.name)
            return
        # self.debug(
        #     f"OK: ge.SetupCameraInterface({ge.connectionType_Ethernet}, "
        #     + f"ipaddress={self.network.ipaddr}, addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')"
        # )

        ret = ge.ConnectToSingleCameraServer(addr=self.ge_device)
        if not ret:
            self.error(
                f"could not ge.ConnectToSingleCameraServer(addr={self.ge_device}) ipaddr='{self.network.ipaddr}' "
                + f"(ret={ret}, msg='{ge.StatusMSG}')"
            )
            self.end_activity(GreatEyesActivities.Probing, label=self.name)
            return
        # self.debug(
        #     f"OK: ge.ConnectToSingleCameraServer(addr={self.ge_device}) "
        #     + f"(ret={ret}, msg='{ge.StatusMSG}')"
        # )

        model = []
        ret = ge.ConnectCamera(model=model, addr=self.ge_device)
        if not ret:
            self.error(
                f"could not ge.ConnectCamera(model=[], addr={self.ge_device}) (ret={ret}, "
                + f"msg='{ge.StatusMSG}')"
            )
            self.end_activity(GreatEyesActivities.Probing, label=self.name)
            return
        fw_version = ge.GetFirmwareVersion(self.ge_device)
        self.debug(
            f"OK: ge.ConnectCamera(model={model}, ipaddr='{self.network.ipaddr}' addr={self.ge_device} fw={fw_version}) (ret={ret}, msg='{ge.StatusMSG}')"
        )

        self.model_id = model[0]
        self.model = model[1]

        self._connected = True
        self._detected = True

    def probe(self):
        """
        Tries to detect the camera
        """
        assert self.power_switch
        if not self.power_switch.detected:
            return

        if not self.enabled or self.detected:
            return

        self.start_activity(GreatEyesActivities.Probing, label=self._name)
        self.try_connect_camera()

        assert self.conf.settings is not None
        default_settings = GreateyesSettingsModel(**self.conf.settings.model_dump())
        if not self.detected:
            if self.is_off():
                self.info("powering ON")
                self.power_on()
            else:
                self.info("cycling power")
                self.cycle()
            assert default_settings.probing
            boot_delay = default_settings.probing.boot_delay
            self.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
            assert boot_delay
            time.sleep(boot_delay)

            self.try_connect_camera()
            if not self.detected:
                self.end_activity(GreatEyesActivities.Probing, label=self.name)
                return

        assert self.ge_device is not None
        self.firmware_version = ge.GetFirmwareVersion(addr=self.ge_device)

        ret = ge.InitCamera(addr=self.ge_device)
        if not ret:
            self.error(
                f"FAILED - ge.InitCamera(addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')"
            )
            ge.DisconnectCamera(addr=self.ge_device)
            ge.DisconnectCameraServer(addr=self.ge_device)
            self.end_activity(GreatEyesActivities.Probing, label=self.name)
            return

        # NOTE: The number 42223 is from the file received with the cameras on the USB stick
        info = ge.TemperatureControl_Init(coolingHardware=42223, addr=self.ge_device)
        self.min_temp = info[0]
        self.max_temp = info[1]

        info = ge.GetImageSize(addr=self.ge_device)
        self.x_size = info[0]
        self.y_size = info[1]
        self.bytes_per_pixel = info[2]

        self.pixel_size_microns = ge.GetSizeOfPixel(addr=self.ge_device)

        self.info(
            f"greateyes: ipaddr='{self.network.ipaddr}', size={self.x_size}x{self.y_size}, "
            + f"model_id='{self.model_id}', model='{self.model}', fw_version='{self.firmware_version}'"
        )

        n_output_modes = ge.GetNumberOfSensorOutputModes(addr=self.ge_device)
        for n in range(n_output_modes):
            mode = ge.GetSensorOutputModeStrings(n, addr=self.ge_device)
            self.info(f"supported output mode[{n}]: '{mode}'")
            self.output_modes.append(mode)

        self.apply_settings(default_settings)

        self.set_led(False)
        self.end_activity(GreatEyesActivities.Probing, label=self.name)

    @property
    def detected(self) -> bool:
        return self._detected

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def was_shut_down(self):
        return self._was_shut_down

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def set_led(self, on_off: bool):
        if not self.detected:
            return

        assert self.ge_device is not None
        ret = ge.SetLEDStatus(on_off, addr=self.ge_device)
        if not ret:
            self.error(f"could not set back LED to {'ON' if on_off else 'OFF'}")

    def __repr__(self):
        return (
            f"<Greateyes>(band={self.band}, id={self.band}, address='{self.network.ipaddr}', model='{self.model}', "
            + f"model_id='{self.model_id}', firmware_version={self.firmware_version})"
        )

    def __del__(self):
        if self.ge_device is None:
            return
        if not self.detected:
            return
        ge.DisconnectCamera(addr=self.ge_device)
        ge.DisconnectCameraServer(addr=self.ge_device)

    def append_error(self, err):
        self.errors.append(err)
        self.error(err)

    def status(self) -> GreateyesStatus:
        assert self.ge_device is not None

        front_temperature = (
            ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.ge_device)
            if self.connected
            else None
        )
        back_temperature = (
            ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.ge_device)
            if self.connected
            else None
        )

        ret = GreateyesStatus(
            powered=self.is_on(),
            band=self.band,
            ipaddr=self.network.ipaddr,
            enabled=self.enabled,
            detected=self.detected,
            connected=self.connected,
            addr=self.ge_device,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            min_temp=self.min_temp,
            max_temp=self.max_temp,
            front_temperature=front_temperature,
            back_temperature=back_temperature,
            errors=self.errors,
            latest_exposure=self.latest_exposure.to_dict()
            if self.latest_exposure
            else None,
            latest_settings=self.latest_settings.model_dump()
            if self.latest_settings
            else None,
        )

        return ret

    def cool_down(self):
        if not self.detected:
            return

        assert self.ge_device is not None
        assert self.settings.temp and self.settings.temp.target_cool
        if ge.TemperatureControl_SetTemperature(
            temperature=self.settings.temp.target_cool, addr=self.ge_device
        ):
            self.start_activity(
                GreatEyesActivities.CoolingDown,
                label=self._name,
                details=[f"to {self.settings.temp.target_cool}°C"],
            )

    def warm_up(self):
        if not self.detected:
            return
        assert self.ge_device is not None
        if ge.TemperatureControl_SetTemperature(
            temperature=self.max_temp, addr=self.ge_device
        ):
            self.start_activity(GreatEyesActivities.WarmingUp, label=self._name)

    def startup(self):
        if not self.detected:
            return
        if OperatingMode().production_mode:
            self.start_activity(GreatEyesActivities.StartingUp, label=self._name)
            self.cool_down()
        else:
            self.info("MAST_DEBUG is set, not cooling down on startup")
        self._was_shut_down = False

    def shutdown(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.ShuttingDown, label=self._name)
        if self.is_active(GreatEyesActivities.Exposing):
            self.abort()
        if OperatingMode().production_mode:
            self.warm_up()
        else:
            self.info("MAST_DEBUG is set, not warming up on shutdown")
        self.shutdown_event.set()
        self._was_shut_down = True

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(GreatEyesActivities.ShuttingDown)

    def _apply_setting(self, func: Callable, arg):
        op = f"{func.__name__ if hasattr(func, '__name__') else str(func)}({arg}, addr={self.ge_device})"
        ret = (
            func(*arg, addr=self.ge_device)
            if isinstance(arg, (tuple, list))
            else func(arg, addr=self.ge_device)
        )
        if ret:
            self.info(f"OK - {op}")
        else:
            self.append_error(f"FAILED - {op} (status: {ge.StatusMSG} ({ge.Status}))")
        return ret

    def apply_settings(self, settings: GreateyesSettingsModel):
        """
        Enforces settings from an assignment onto this specific camera.
        :param settings: e.g. from an assignment
        :return:
        """

        self.errors = []
        if not self.detected:
            self.errors.append("not detected")
            return

        # print("apply_settings:\n" + settings.model_dump_json(indent=2))
        self.start_activity(GreatEyesActivities.SettingParameters, label=self.name)
        self._apply_setting(
            ge.SetBitDepth, settings.bytes_per_pixel or self.conf.bytes_per_pixel
        )

        # Note: SetupSensorOutputMode always returns False (even their example ignores the return value)
        assert settings.readout
        assert self.ge_device is not None
        readout_mode = (
            settings.readout.mode.value
            if settings.readout.mode is not None
            else self.conf.readout.mode.value
        )
        ge.SetupSensorOutputMode(readout_mode, addr=self.ge_device)
        self.info(
            f"OK - SetupSensorOutputMode({readout_mode}, addr={self.ge_device}) (ret value ignored)"
        )
        info = ge.GetImageSize(addr=self.ge_device)
        if (
            info[0] != self.x_size
            or info[1] != self.y_size
            or info[2] != self.bytes_per_pixel
        ):
            self.warning(
                f"image size changed after setting output mode: was {self.x_size} x {self.y_size} x {self.bytes_per_pixel}, now {info[0]} x {info[1]} x {info[2]}"
            )
            self.x_size = info[0]
            self.y_size = info[1]
            self.bytes_per_pixel = info[2]

        self._apply_setting(
            ge.SetReadOutSpeed,
            settings.readout.speed.value
            if settings.readout.speed is not None
            else self.conf.readout.speed.value,
        )

        binning_x = (
            settings.binning.x if settings.binning is not None else self.conf.binning.x
        )
        binning_y = (
            settings.binning.y if settings.binning is not None else self.conf.binning.y
        )
        self._apply_setting(ge.SetBinningMode, (binning_x, binning_y))

        if settings.crop is not None:
            if settings.crop.enabled:
                self._apply_setting(
                    ge.SetupCropMode2D, (settings.crop.col, settings.crop.line)
                )
                self._apply_setting(ge.ActivateCropMode, True)
        elif self.conf.crop is not None:
            if self.conf.crop.enabled:
                self._apply_setting(
                    ge.SetupCropMode2D, (self.conf.crop.col, self.conf.crop.line)
                )
                self._apply_setting(ge.ActivateCropMode, True)
        else:
            self._apply_setting(ge.ActivateCropMode, False)

        if settings.shutter is not None and settings.shutter.automatic:
            self._apply_setting(
                ge.SetShutterTimings,
                (settings.shutter.open_time, settings.shutter.close_time),
            )
        elif self.conf.shutter is not None and self.conf.shutter.automatic:
            self._apply_setting(
                ge.SetShutterTimings,
                (self.conf.shutter.open_time, self.conf.shutter.close_time),
            )

        self.latest_settings = settings
        self.end_activity(GreatEyesActivities.SettingParameters, label=self.name)

    def start_exposure(self, settings: GreateyesSettingsModel):
        self.errors = []
        if not self.detected:
            self.errors.append("not detected")
            return

        assert self.ge_device is not None
        if not self.is_idle():
            if ge.DllIsBusy(addr=self.ge_device):
                self.append_error(
                    f"could not start exposure: ge.DllIsBusy(addr={self.ge_device})"
                )
                return

            if self.is_active(GreatEyesActivities.CoolingDown):
                ret = ge.TemperatureControl_GetTemperature(
                    thermistor=0, addr=self.ge_device
                )
                if ret == FAILED_TEMPERATURE:
                    self.append_error(f"could not read sensor temperature ({ret=})")
                else:
                    assert self.settings.temp
                    delta_temp = abs(self.settings.temp.target_cool - ret)
                    self.append_error(
                        f"cannot expose while cooling down ({delta_temp=} deg to cool)"
                    )
                return

            if not self.is_idle():
                self.append_error(f"camera is active ({self.activities=})")
                return

        self.latest_exposure.settings = settings

        self.start_activity(GreatEyesActivities.Acquiring, label=self.name)
        assert self.latest_settings and self.latest_settings.readout
        if 0 < self.latest_settings.readout.mode >= len(self.output_modes):
            self.append_error(
                f"{self.latest_settings.readout.mode=} is not in range({len(self.output_modes)}"
            )
        else:
            ret = ge.SetupSensorOutputMode(
                self.latest_settings.readout.mode, addr=self.ge_device
            )
            # if not ret:
            #     self.append_error(
            #         f"could not SetupSensorOutputMode({self.latest_settings.readout.mode}) (error: {ge.StatusMSG})"
            #     )

            info = ge.GetImageSize(addr=self.ge_device)
            if info[0] != self.x_size or info[1] != self.y_size:
                self.warning(
                    f"image size changed after setting output mode: was {self.x_size} x {self.y_size}, now {info[0]} x {info[1]}"
                )
                self.x_size = info[0]
                self.y_size = info[1]

        assert self.latest_exposure.settings.exposure_duration
        if not self._apply_setting(
            ge.SetExposure, int(self.latest_exposure.settings.exposure_duration * 1000)
        ):
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            return

        assert self.latest_settings.shutter
        mode = 2 if self.latest_settings.shutter.automatic else 1
        self._apply_setting(ge.OpenShutter, mode)

        ret = ge.StartMeasurement_DynBitDepth(
            addr=self.ge_device, showShutter=self.latest_settings.shutter.automatic
        )
        if ret:
            self.start_activity(GreatEyesActivities.Exposing, label=self.name)
            assert self.latest_exposure.timing
            self.latest_exposure.timing.start_utc = datetime.datetime.now(datetime.UTC)
            self.latest_exposure.timing.start = datetime.datetime.now()
        else:
            self.append_error(
                f"could not ge.StartMeasurement_DynBitDepth(addr={self.ge_device}) ({ret=})"
            )

    def readout(self):
        if not self.detected:
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            return

        assert self.latest_exposure.settings
        if not self.latest_exposure.settings.image_file:
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            raise Exception("empty self.latest_exposure.settings.image_file")

        assert self.ge_device is not None
        self.start_activity(GreatEyesActivities.ReadingOut, label=self.name)
        image_array = ge.GetMeasurementData_DynBitDepth(addr=self.ge_device)
        self.end_activity(GreatEyesActivities.ReadingOut, label=self.name)

        assert self.latest_settings and self.latest_settings.shutter
        if not self.latest_settings.shutter.automatic:
            ret = ge.OpenShutter(0, addr=self.ge_device)
            if not ret:
                self.append_error(
                    f"could not close shutter with ge.OpenShutter(0, addr={self.ge_device})"
                )

        self.start_activity(GreatEyesActivities.Saving, label=self.name)
        hdr = fits.Header()
        hdr.append(Card("INSTRUME", "DEEPSPEC", "Instrument"))
        hdr.append(Card("TELESCOP", "WAO-MAST", "Telescope"))
        hdr.append(Card("DETECTOR", "DEEPSPEC", "Detector"))
        hdr.append(Card("BAND", f"DeepSpec-{self.band}", "DEEPSPEC BAND"))
        hdr.append(Card("CAM_IP", self.network.ipaddr, "Camera IP address"))
        hdr.append(Card("TYPE", "RAW", "Exposure type"))

        assert self.latest_exposure.timing
        hdr.append(
            Card(
                "LT_START",
                self.latest_exposure.timing.start.strftime(FITS_DATE_FORMAT),
                "Exposure time start (local)",
            )
        )
        hdr.append(
            Card(
                "LT_MID",
                self.latest_exposure.timing.mid.strftime(FITS_DATE_FORMAT),
                "Exposure mid time (local)",
            )
        )
        hdr.append(
            Card(
                "LT_END",
                self.latest_exposure.timing.end.strftime(FITS_DATE_FORMAT),
                "Exposure end time (local)",
            )
        )

        hdr.append(
            Card(
                "T_START",
                self.latest_exposure.timing.start_utc.strftime(FITS_DATE_FORMAT),
                "Exposure time start (UTC)",
            )
        )
        hdr.append(
            Card(
                "T_MID",
                self.latest_exposure.timing.mid_utc.strftime(FITS_DATE_FORMAT),
                "Exposure mid time (UTC)",
            )
        )
        hdr.append(
            Card(
                "T_END",
                self.latest_exposure.timing.end_utc.strftime(FITS_DATE_FORMAT),
                "Exposure end time (UTC)",
            )
        )

        hdr.append(
            Card(
                "T_EXP",
                self.latest_exposure.settings.exposure_duration,
                "TOTAL INTEGRATION TIME",
            )
        )
        assert self.settings.temp is not None
        hdr.append(
            Card(
                "TEMPGOAL", self.settings.temp.target_cool, "GOAL DETECTOR TEMPERATURE"
            )
        )
        hdr.append(
            Card(
                "TEMPFLAG",
                self.backside_temp_safe,
                "DETECTOR BACKSIDE TEMPERATURE SAFETY FLAG",
            )
        )
        hdr.append(
            Card(
                "DATE-OBS",
                self.latest_exposure.timing.mid_utc.strftime(FITS_DATE_FORMAT),
                "OBSERVATION DATE",
            )
        )
        hdr.append(
            Card(
                "MJD-OBS",
                self.latest_exposure.timing.mid_utc.strftime(FITS_DATE_FORMAT),
                "MJD OF OBSERVATION MIDPOINT",
            )
        )

        if self.latest_exposure.settings.readout is not None:
            hdr.append(
                Card(
                    "RDSPEED",
                    readout_speed_names[self.latest_exposure.settings.readout.speed],
                    "PIXEL READOUT FREQUENCY",
                )
            )

        assert self.latest_exposure.settings.binning is not None
        hdr.append(
            Card(
                "CDELT1",
                self.latest_exposure.settings.binning.x,
                "BINNING IN THE X DIRECTION",
            )
        )
        hdr.append(
            Card(
                "CDELT2",
                self.latest_exposure.settings.binning.y,
                "BINNING IN THE Y DIRECTION",
            )
        )
        hdr.append(Card("NAXIS", 2, "NUMBER OF AXES IN FRAME"))

        assert (
            self.x_size is not None
            and self.y_size is not None
            and self.latest_exposure.settings.binning is not None
        )
        hdr.append(
            Card(
                "NAXIS1",
                self.x_size / self.latest_exposure.settings.binning.x,
                "NUMBER OF PIXELS IN THE X DIRECTION",
            )
        )
        hdr.append(
            Card(
                "NAXIS2",
                self.y_size / self.latest_exposure.settings.binning.y,
                "NUMBER OF PIXELS IN THE Y DIRECTION",
            )
        )
        hdr.append(Card("PIXSIZE", self.pixel_size_microns, "PIXEL SIZE IN MICRONS"))
        hdr.append(
            Card(
                "BITPIX",
                self.latest_settings.bytes_per_pixel,
                "# of bits storing pix values",
            )
        )
        hdu = fits.PrimaryHDU(image_array, header=hdr)
        hdul = fits.HDUList([hdu])

        filename = self.latest_exposure.settings.image_file
        if not filename.endswith(".fits"):
            filename += ".fits"
        try:
            self.start_activity(GreatEyesActivities.Saving, label=self.name)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            hdul.writeto(filename)
            self.end_activity(GreatEyesActivities.Saving, label=self.name)
            self.info(f"saved exposure to '{filename}'")
        except Exception as e:
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            self.debug(f"failed to save exposure (error: {e})")
        self.end_activity(GreatEyesActivities.Acquiring, label=self.name)

    @property
    def is_working(self) -> bool:
        return self.is_active(GreatEyesActivities.Acquiring)

    def abort(self):
        if not self.detected:
            return

        assert self.ge_device is not None
        if not ge.DllIsBusy(addr=self.ge_device):
            return
        ret = ge.StopMeasurement(addr=self.ge_device)
        if not ret:
            self.append_error(f"could not ge.StopMeasurement(addr={self.ge_device})")

    def on_timer(self):
        """
        Called periodically by a timer.
        Checks if any in-progress activities can be ended.
        """

        if not self.settings.enabled:
            return

        if self.shutdown_event.is_set():
            self.timer.cancel()
            return

        assert self.ge_device is not None
        assert (
            self.settings.probing is not None
            and self.settings.probing.interval is not None
        )
        if (
            not self.is_active(GreatEyesActivities.Probing)
            and not self.detected
            and (
                self.last_probe_time is None
                or datetime.datetime.now() - self.last_probe_time
                > datetime.timedelta(seconds=self.settings.probing.interval)
            )
        ):
            self.last_probe_time = datetime.datetime.now()
            self.probe()
            return

        if not self.detected:
            return

        now = datetime.datetime.now()
        assert self.settings.temp is not None
        if self.last_backside_temp_check is None or (
            now - self.last_backside_temp_check
        ) > datetime.timedelta(seconds=self.settings.temp.check_interval):
            ret = ge.TemperatureControl_GetTemperature(
                thermistor=1, addr=self.ge_device
            )
            if ret == FAILED_TEMPERATURE:
                self.error(f"failed to read back temperature ({ret=})")
            else:
                if ret >= 55:
                    self.backside_temp_safe = False
                    self.error(f"back side temperature too high: {ret} degrees celsius")
                else:
                    self.backside_temp_safe = True

            self.last_backside_temp_check = now

        if self.is_active(GreatEyesActivities.Exposing):
            # check if exposure has ended
            if not ge.DllIsBusy(addr=self.ge_device):
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)

                assert self.latest_exposure.timing is not None
                self.latest_exposure.timing.end = datetime.datetime.now()
                self.latest_exposure.timing.mid = (
                    self.latest_exposure.timing.start
                    + (
                        self.latest_exposure.timing.end
                        - self.latest_exposure.timing.start
                    )
                    / 2
                )

                self.latest_exposure.timing.end_utc = (
                    self.latest_exposure.timing.end.astimezone(timezone.utc)
                )
                self.latest_exposure.timing.mid_utc = (
                    self.latest_exposure.timing.mid.astimezone(timezone.utc)
                )
                self.readout_thread = threading.Thread(
                    name=f"deepspec-camera-{self.band}-readout-thread",
                    target=self.readout,
                )
                self.readout_thread.start()
            # else:
            #     elapsed = (
            #         now - self.timings[GreatEyesActivities.Exposing].start_time
            #     ).seconds
            #     assert (
            #         self.latest_exposure.settings is not None
            #         and self.latest_exposure.settings.exposure_duration is not None
            #     )
            #     max_expected = self.latest_exposure.settings.exposure_duration * 10
            #     if elapsed > max_expected:
            #         self.append_error(
            #             f"exposure takes too long ({elapsed=} > {max_expected=})"
            #         )
            #         ret = ge.StopMeasurement(addr=self.ge_device)
            #         if ret:
            #             self.append_error(
            #                 f"could not ge.StopMeasurement(addr={self.ge_device}) ({ret=})"
            #             )
            #         else:
            #             self.end_activity(GreatEyesActivities.Exposing, label=self.name)

        if self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(
            GreatEyesActivities.WarmingUp
        ):
            front_temp = ge.TemperatureControl_GetTemperature(
                thermistor=0, addr=self.ge_device
            )
            if front_temp == FAILED_TEMPERATURE:
                self.append_error("failed reading sensor temperature")
            else:
                switch_temp_control_off = False
                should_power_off = False
                if (
                    self.is_active(GreatEyesActivities.CoolingDown)
                    and abs(front_temp - self.settings.temp.target_cool) <= 1
                ):
                    self.end_activity(GreatEyesActivities.CoolingDown, label=self._name)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(
                            GreatEyesActivities.StartingUp, label=self._name
                        )
                    switch_temp_control_off = True

                if (
                    self.is_active(GreatEyesActivities.WarmingUp)
                    and abs(front_temp >= self.settings.temp.target_warm) <= 1
                ):
                    self.end_activity(GreatEyesActivities.WarmingUp, label=self._name)
                    if self.is_active(GreatEyesActivities.ShuttingDown):
                        self.end_activity(
                            GreatEyesActivities.ShuttingDown, label=self._name
                        )
                        should_power_off = True
                    switch_temp_control_off = True

                if switch_temp_control_off:
                    ret = ge.TemperatureControl_SwitchOff(addr=self.ge_device)
                    if ret:
                        self.info(
                            f"OK: ge.TemperatureControl_SwitchOff(addr={self.ge_device})"
                        )
                    else:
                        self.error(
                            f"could not ge.TemperatureControl_SwitchOff(addr={self.ge_device}) (ret={ret})"
                        )

                if should_power_off:
                    self.power_off()
                    self.timer.finished.set()

    @property
    def operational(self) -> bool:
        if not self.enabled:
            return False

        assert self.power_switch is not None
        return (
            self.power_switch.detected
            and self.detected
            and not (
                self.is_active(GreatEyesActivities.CoolingDown)
                or self.is_active(GreatEyesActivities.WarmingUp)
            )
        )

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        label = f"{self._name}:"

        if not self.enabled:
            ret.append(f"{label} disabled")
            return ret

        assert self.power_switch is not None
        if not self.power_switch.detected:
            ret.append(f"{label} {self.power_switch} not detected")
        elif self.is_off():
            ret.append(f"{label} {self.power_switch}:{self.outlet_name} is OFF")
        else:
            if not self.detected:
                ret.append(f"{label} camera (at {self.network.ipaddr}) not detected")
            if self.is_active(GreatEyesActivities.CoolingDown):
                ret.append(f"{label} camera is CoolingDown")
            if self.is_active(GreatEyesActivities.WarmingUp):
                ret.append(f"{label} camera is WarmingUp")

        return ret

    def error(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self._name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self._name}: ",)

        logger.error(*args, **kwargs)

    def warning(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self._name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self._name}: ",)

        logger.warning(*args, **kwargs)

    def info(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self._name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self._name}: ",)

        logger.info(*args, **kwargs)

    def debug(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self._name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self._name}: ",)

        logger.debug(*args, **kwargs)

    def do_execute_assignment(
        self, assignment: SpectrographAssignmentModel, folder: str
    ):
        assert isinstance(assignment.spec, DeepspecModel)
        deepspec_assignment: DeepspecModel = assignment.spec

        assert deepspec_assignment.camera is not None
        settings: GreateyesSettingsModel = deepspec_assignment.camera[self.band]

        self.apply_settings(settings=settings)

        assert settings.number_of_exposures is not None
        for exposure_number in range(1, settings.number_of_exposures + 1):
            settings.image_file = os.path.join(
                folder, f"exposure-{exposure_number:03}.fits"
            )
            self.start_exposure(settings)
            while self.is_active(GreatEyesActivities.Acquiring):
                time.sleep(0.5)

    def execute_assignment(self, assignment: SpectrographAssignmentModel, folder: str):
        threading.Thread(
            target=self.do_execute_assignment, args=[assignment, folder]
        ).start()


class GreateyesFactory:
    _instances: dict[DeepspecBands, GreatEyes | None] = {
        "I": None,
        "G": None,
        "R": None,
        "U": None,
    }

    @classmethod
    def get_instance(cls, band: DeepspecBands) -> GreatEyes | None:
        if not cls._instances[band]:
            cls._instances[band] = GreatEyes(band=band)
        return cls._instances[band]


def make_camera(band: DeepspecBands):
    op = function_name()
    try:
        cameras[band] = GreateyesFactory.get_instance(band=band)
    except Exception as e:
        logger.error(f"{op}: caught {e}")
        cameras[band] = None


cameras: dict[str, GreatEyes | None] = {}

for _band in list(get_args(DeepspecBands)):
    threading.Thread(
        name=f"make-deepspec-camera-{_band}", target=make_camera, args=[_band]
    ).start()


if __name__ == "__main__":
    for c in cameras:
        print(cameras[c])
