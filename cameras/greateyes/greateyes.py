import datetime
import os
import sys
import threading
import time
from datetime import timezone
from enum import IntEnum
from typing import Callable, get_args

import astropy.io.fits as fits
import numpy as np
from astropy.io.fits import Card
from pydantic import BaseModel

from common.activities import GreatEyesActivities
from common.config import Config
from common.dlipowerswitch import SwitchedOutlet
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.assignments import SpectrographAssignment
from common.models.deepspec import DeepspecSettings
from common.models.greateyes import (
    GreateyesSettingsModel,
    ReadoutSpeed,
)
from common.models.statuses import GreateyesStatus
from common.networking import NetworkedDevice
from common.spec import DeepspecBands, FrameType, SpecExposureSettings
from common.utils import OperatingMode, RepeatTimer, function_name

sys.path.append(os.path.join(os.path.dirname(__file__), "sdk"))
import cameras.greateyes.sdk.greateyesSDK as ge

logger = get_logger(__name__)
dll_version = ge.GetDLLVersion()
shown_dll_version = False

if not shown_dll_version:
    logger.info(f"Greateyes DLL version: '{dll_version}'")
    shown_dll_version = True

FAILED_TEMPERATURE = -300

FITS_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


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
        self.conf = Config().get_specs().deepspec[self.band]  # specific to this camera instance
        assert self.conf.settings is not None
        self.greateyes_settings: GreateyesSettingsModel = GreateyesSettingsModel(**self.conf.settings.model_dump())
        self.latest_spec_exposure_settings: SpecExposureSettings | None = None
        self.latest_greateyes_exposure_settings: GreateyesSettingsModel | None = None
        # Path save_image() actually wrote, which may differ from the requested image_file.
        self.latest_saved_image_path: str | None = None

        self.ge_device = self.conf.device
        self._name = f"Deepspec-{self.band}"
        self.outlet_name = f"Deepspec{self.band}"
        self.errors = []
        self.output_modes: list[str] = []
        self.sensor_temperature_target: float | None = None

        from common.dlipowerswitch import OutletDomain, SwitchedOutlet

        assert self.conf.network is not None
        NetworkedDevice.__init__(self, self.conf.model_dump())
        SwitchedOutlet.__init__(self, outlet_name=f"{self.outlet_name}", domain=OutletDomain.SpecOutlets)

        self.greateyes_settings: GreateyesSettingsModel = GreateyesSettingsModel(**self.conf.settings.model_dump())

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
        self.timer_frequency: float = 1  # [seconds] how often to check the camera status, e.g. backside temperature
        self.timer = RepeatTimer(self.timer_frequency, function=self.on_timer)
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
            self.error(f"ge.DisconnectCameraServer(addr={self.ge_device}) caught error {e}, ignoring.")
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
                f"could not ge.ConnectCamera(model=[], addr={self.ge_device}) (ret={ret}, " + f"msg='{ge.StatusMSG}')"
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
            self.warning(f"power switch {self.power_switch} not detected")

        if not self.enabled or self.detected:
            return

        self.start_activity(
            GreatEyesActivities.Probing,
            label=self._name,
            details=[f"band={self.band}", f"ipaddr={self.network.ipaddr}"],
        )
        self.try_connect_camera()

        assert self.conf.settings is not None
        default_settings = GreateyesSettingsModel(**self.conf.settings.model_dump())
        if not self.detected:
            if self.power_switch is not None and self.power_switch.detected:
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
            else:
                self.warning(
                    f"power switch {self.power_switch} not detected, skipping power cycle, will try to connect to the camera anyway"
                )

            self.try_connect_camera()
            if not self.detected:
                self.end_activity(GreatEyesActivities.Probing, label=self.name)
                return

        assert self.ge_device is not None
        self.firmware_version = ge.GetFirmwareVersion(addr=self.ge_device)

        ret = ge.InitCamera(addr=self.ge_device)
        if not ret:
            self.error(f"FAILED - ge.InitCamera(addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")
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
            + f"model_id='{self.model_id}', model='{self.model}', fw_version='{self.firmware_version}', "
            + f"sensor temp range={self.min_temp}ֲ°C to {self.max_temp}ֲ°C"
        )

        n_output_modes = ge.GetNumberOfSensorOutputModes(addr=self.ge_device)
        for n in range(n_output_modes):
            mode = ge.GetSensorOutputModeStrings(n, addr=self.ge_device)
            self.info(f"supported output mode[{n}]: '{mode}'")
            self.output_modes.append(mode)

        # Nothing is applied here any more. Probing used to end by pushing the config
        # defaults onto the camera, which meant the hardware carried settings nobody had
        # asked for and every later exposure inherited whatever the last one left. Each
        # exposure now applies its own, in start_exposure. The image geometry this class
        # reports was read straight from the camera above, so it is already current.
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
        op = f"ge.SetLEDStatus({'ON' if on_off else 'OFF'}, addr={self.ge_device})"
        ret = ge.SetLEDStatus(on_off, addr=self.ge_device)
        if ret:
            self.info(f"OK - {op}")
        else:
            self.error(f"FAILED - {op} (status: {ge.StatusMSG} ({ge.Status}))")

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

        sensor_temperature = self.get_sensor_temperature()
        back_temperature = self.get_back_temperature()

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
            sensor_temperature=sensor_temperature,
            back_temperature=back_temperature,
            errors=self.errors,
            latest_exposure=self.latest_exposure.to_dict() if self.latest_exposure else None,
            latest_spec_exposure_settings=self.latest_spec_exposure_settings if self.latest_spec_exposure_settings else None,
            sensor_temperature_target=self.sensor_temperature_target,
        )

        return ret

    def adjust_temperature(self, target_temperature: int):
        if not self.detected:
            return

        assert self.ge_device is not None
        self.sensor_temperature_target = target_temperature
        if ge.TemperatureControl_SetTemperature(temperature=target_temperature, addr=self.ge_device):
            self.start_activity(
                GreatEyesActivities.AdjustingTemperature,
                label=self._name,
                details=[f"to {target_temperature}ֲ°C"],
            )
        else:
            self.append_error(
                f"FAILED to set temperature to {target_temperature}ֲ°C with ge.TemperatureControl_SetTemperature (status: {ge.StatusMSG} ({ge.Status}))"
            )

    def cool_down(self):
        if not self.detected:
            return

        assert self.ge_device is not None
        assert self.greateyes_settings.temp and self.greateyes_settings.temp.target_cool

        target_temp = self.greateyes_settings.temp.target_cool
        self.sensor_temperature_target = target_temp
        if self._apply_setting(ge.TemperatureControl_SetTemperature, target_temp):
            self.start_activity(
                GreatEyesActivities.CoolingDown,
                label=self._name,
                details=[f"to {target_temp}ֲ°C"],
            )

    def warm_up(self):
        if not self.detected:
            return
        assert self.ge_device is not None

        target_temp = self.max_temp if self.max_temp is not None else 20
        if self._apply_setting(ge.TemperatureControl_SetTemperature, target_temp):
            self.start_activity(
                GreatEyesActivities.WarmingUp,
                label=self._name,
                details=[f"to {target_temp}ֲ°C"],
            )

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
        ret = func(*arg, addr=self.ge_device) if isinstance(arg, (tuple, list)) else func(arg, addr=self.ge_device)
        if ret:
            self.info(f"OK - {op}")
        else:
            self.append_error(f"FAILED - {op} (status: {ge.StatusMSG} ({ge.Status}))")
        return ret

    def start_exposure(
        self,
        greateyes_exposure_settings: GreateyesSettingsModel,
        bypass_temperature_stabilization_check: bool = False,
    ):
        self.errors = []
        if not self.detected:
            self.errors.append("not detected")
            return

        assert self.ge_device is not None
        if ge.DllIsBusy(addr=self.ge_device):
            self.append_error(f"could not start exposure: ge.DllIsBusy(addr={self.ge_device})")
            return

        if bypass_temperature_stabilization_check:
            exposure_blocking = {
                GreatEyesActivities.Acquiring,
                GreatEyesActivities.Exposing,
                GreatEyesActivities.ReadingOut,
                GreatEyesActivities.Saving,
            }
            if any(self.is_active(a) for a in exposure_blocking):
                self.append_error(f"camera is active ({self.activities=})")
                return
        elif not self.is_idle():
            self.append_error(f"camera is active ({self.activities=})")
            return

        self.latest_exposure.settings = greateyes_exposure_settings
        assert isinstance(greateyes_exposure_settings.exposure_duration, (int, float))
        assert greateyes_exposure_settings.binning is not None

        self.latest_spec_exposure_settings = SpecExposureSettings(
            exposure_duration=greateyes_exposure_settings.exposure_duration,
            x_binning=greateyes_exposure_settings.binning.x,
            y_binning=greateyes_exposure_settings.binning.y,
            number_of_exposures=greateyes_exposure_settings.number_of_exposures,
            frame_type=greateyes_exposure_settings.frame_type,
        )

        self.start_activity(GreatEyesActivities.Acquiring, label=self.name)
        assert self.latest_spec_exposure_settings and greateyes_exposure_settings.readout
        assert self.ge_device is not None

        # SettingParameters still bracket the settings, as they did in apply_settings, so
        # the status surface keeps reporting the phase. It now sits inside Acquiring, which
        # is where the work actually happens.
        self.start_activity(GreatEyesActivities.SettingParameters, label=self.name)

        # Every setting this exposure depends on is applied here, from the settings this
        # exposure was given. It used to be split: apply_settings() carried bit depth,
        # binning, crop and shutter timings, and was called only from the probe (with
        # config defaults) and from do_execute_assignment. The manual `deepspec/expose`
        # endpoint went straight here, so its x_binning/y_binning were accepted, packed
        # into the model and silently never applied -- frames came out at whatever the
        # last caller had left on the camera.
        # `conf` is the GreateyesConfig -- network, power, enabled, device, settings. The
        # settings themselves live one level down, so every fallback below reads
        # `conf.settings.X`. The code this replaces said `conf.X`, which would have raised
        # AttributeError, but only on a branch nothing had ever taken: those fallbacks are
        # reached only when the exposure's own model leaves a field None, and the two
        # callers apply_settings had always filled them in. `deepspec/expose` builds its
        # model with `crop=None`, so consolidating here is what finally evaluated them.
        conf = self.conf.settings
        assert conf is not None

        self._apply_setting(
            ge.SetBitDepth,
            greateyes_exposure_settings.bytes_per_pixel or conf.bytes_per_pixel,
        )

        if 0 < greateyes_exposure_settings.readout.mode >= len(self.output_modes):
            self.append_error(f"{greateyes_exposure_settings.readout.mode=} is not in range({len(self.output_modes)}")
        else:
            # Called bare, and NOT through _apply_setting: SetupSensorOutputMode always
            # returns False (the vendor's own example ignores the return value), so
            # _apply_setting read every call as a failure and appended an error. That error
            # was harmless to the exposure and fatal to the products -- expose_one_camera
            # bails on `camera.errors`, and its `finally` then reaped the folder while the
            # camera was still exposing. The frames landed in a folder nobody owned and
            # were never moved to the shared area.
            readout_mode = greateyes_exposure_settings.readout.mode.value
            ge.SetupSensorOutputMode(readout_mode, addr=self.ge_device)
            self.info(f"OK - SetupSensorOutputMode({readout_mode}, addr={self.ge_device}) (ret value ignored)")

            info = ge.GetImageSize(addr=self.ge_device)
            if info[0] != self.x_size or info[1] != self.y_size or info[2] != self.bytes_per_pixel:
                self.warning(
                    f"image size changed after setting output mode: was {self.x_size} x {self.y_size} x {self.bytes_per_pixel}, now {info[0]} x {info[1]} x {info[2]}"
                )
                self.x_size = info[0]
                self.y_size = info[1]
                self.bytes_per_pixel = info[2]

        readout_speed = (
            greateyes_exposure_settings.readout.speed.value
            if greateyes_exposure_settings.readout and greateyes_exposure_settings.readout.speed is not None
            else conf.readout.speed.value
        )
        self._apply_setting(ge.SetReadOutSpeed, readout_speed)

        binning = greateyes_exposure_settings.binning
        self._apply_setting(
            ge.SetBinningMode,
            (
                binning.x if binning is not None else conf.binning.x,
                binning.y if binning is not None else conf.binning.y,
            ),
        )

        # Deliberately stricter than the code this replaces, which left crop mode untouched
        # when a settings model carried `crop.enabled = False` -- so an exposure that asked
        # for no cropping inherited whatever the previous one had switched on. Every branch
        # now states what it wants.
        crop = greateyes_exposure_settings.crop if greateyes_exposure_settings.crop is not None else conf.crop
        if crop is not None and crop.enabled:
            self._apply_setting(ge.SetupCropMode2D, (crop.col, crop.line))
            self._apply_setting(ge.ActivateCropMode, True)
        else:
            self._apply_setting(ge.ActivateCropMode, False)

        shutter = greateyes_exposure_settings.shutter if greateyes_exposure_settings.shutter is not None else conf.shutter
        if shutter is not None and shutter.automatic:
            self._apply_setting(ge.SetShutterTimings, (shutter.open_time, shutter.close_time))

        self.end_activity(GreatEyesActivities.SettingParameters, label=self.name)

        assert self.latest_exposure.settings.exposure_duration
        if not self._apply_setting(ge.SetExposure, int(self.latest_exposure.settings.exposure_duration * 1000)):
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            return

        assert greateyes_exposure_settings.shutter
        mode = 2 if greateyes_exposure_settings.shutter.automatic else 1
        self._apply_setting(ge.OpenShutter, mode)
        ret = ge.StartMeasurement_DynBitDepth(addr=self.ge_device)
        op = f"StartMeasurement_DynBitDepth(addr={self.ge_device})"
        if ret:
            self.info(f"OK - {op}")
            self.start_activity(GreatEyesActivities.Exposing, label=self.name)
            assert self.latest_exposure.timing
            self.latest_exposure.timing.start_utc = datetime.datetime.now(datetime.UTC)
            self.latest_exposure.timing.start = datetime.datetime.now()
            self.latest_greateyes_exposure_settings = greateyes_exposure_settings
        else:
            self.append_error(f"FAILED - {op} (status: {ge.StatusMSG} ({ge.Status}))")
            # Acquiring was set on the way in and no readout thread is coming to clear it,
            # since the measurement never started. Left set, it wedges the camera: is_idle()
            # refuses the next exposure, and anything polling `while is_active(Acquiring)`
            # -- Deepspec.expose_one_camera does -- waits for ever. The SetExposure failure
            # above already ends it for the same reason.
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)

    def _close_shutter_if_manual(self):
        """Close the shutter, unless the camera is driving it automatically."""
        assert self.ge_device is not None
        settings = self.latest_greateyes_exposure_settings
        if not settings or not settings.shutter or settings.shutter.automatic:
            return
        if not ge.OpenShutter(0, addr=self.ge_device):
            self.append_error(f"could not close shutter with ge.OpenShutter(0, addr={self.ge_device})")

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

        assert self.latest_greateyes_exposure_settings
        self._close_shutter_if_manual()

        # SDK 22.5 rev2 signals a failed readout with None (and False when the
        # reported bit depth was unusable). Earlier wrappers returned a
        # zero-filled array instead, which we would have written out as a
        # perfectly valid-looking FITS full of zeros. Checked after the shutter
        # block above so a failed readout still closes the shutter, and by type
        # rather than truthiness because a truth test on an ndarray raises.
        if not isinstance(image_array, np.ndarray):
            self.append_error(
                f"FAILED - GetMeasurementData_DynBitDepth(addr={self.ge_device}) (status: {ge.StatusMSG} ({ge.Status}))"
            )
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            return

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
        assert self.greateyes_settings.temp is not None
        hdr.append(
            Card(
                "TEMPGOAL",
                self.greateyes_settings.temp.target_cool,
                "GOAL DETECTOR TEMPERATURE",
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
                "TEMPSENS",
                self.get_sensor_temperature(),
                "DETECTOR SENSOR TEMPERATURE",
            )
        )
        hdr.append(
            Card(
                "TEMPBACK",
                self.get_back_temperature(),
                "DETECTOR BACKSIDE TEMPERATURE",
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
            from common.models.greateyes import readout_amplifier_names

            hdr.append(
                Card(
                    "RDAMPS",
                    readout_amplifier_names[self.latest_exposure.settings.readout.mode],
                    "READOUT AMPLIFIER(S)",
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

        assert self.x_size is not None and self.y_size is not None and self.latest_exposure.settings.binning is not None
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
        if self.latest_greateyes_exposure_settings:
            hdr.append(
                Card(
                    "BITPIX",
                    self.latest_greateyes_exposure_settings.bytes_per_pixel,
                    "# of bits storing pix values",
                )
            )
        hdu = fits.PrimaryHDU(image_array, header=hdr)
        hdul = fits.HDUList([hdu])

        filename = self.latest_exposure.settings.image_file
        if not filename.endswith(".fits"):
            filename += ".fits"
        if self.latest_exposure.settings.frame_type != FrameType.LIGHT:
            filename = filename.replace(".fits", f",{self.latest_exposure.settings.frame_type.value}.fits")
        try:
            self.start_activity(GreatEyesActivities.Saving, label=self.name)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            # Protect the FITS from being moved to shared while it is still being written,
            # and record it as a product so release_folder() waits for it instead of
            # discarding it as scratch. This runs on the camera's acquisition thread; the
            # ram->shared move runs on Filer's mover thread, so the two never deadlock.
            with MoveGuardian().protect(filename):
                hdul.writeto(filename)
            # Record what was actually written, not what was requested: the name above may
            # have gained a ".fits" suffix or a frame-type tag. do_execute_assignment moves
            # this path, so it has to be the real one.
            self.latest_saved_image_path = filename
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

        if not self.greateyes_settings.enabled:
            return

        if self.shutdown_event.is_set():
            self.timer.cancel()
            return

        assert self.ge_device is not None
        assert self.greateyes_settings.probing is not None and self.greateyes_settings.probing.interval is not None
        if (
            not self.is_active(GreatEyesActivities.Probing)
            and not self.detected
            and (
                self.last_probe_time is None
                or datetime.datetime.now() - self.last_probe_time
                > datetime.timedelta(seconds=self.greateyes_settings.probing.interval)
            )
        ):
            self.last_probe_time = datetime.datetime.now()
            self.probe()
            return

        if not self.detected:
            return

        now = datetime.datetime.now()
        assert self.greateyes_settings.temp is not None
        if self.last_backside_temp_check is None or (now - self.last_backside_temp_check) > datetime.timedelta(
            seconds=self.greateyes_settings.temp.check_interval
        ):
            ret = self.get_back_temperature()
            if ret is None:
                # self.error("failed to read back temperature")
                pass
            elif ret >= 55:
                self.backside_temp_safe = False
                self.error(f"back side temperature too high: {ret} degrees celsius")
            else:
                self.backside_temp_safe = True

            self.last_backside_temp_check = now

        if self.is_active(GreatEyesActivities.Exposing):
            if not ge.DllIsBusy(addr=self.ge_device):
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)

                assert self.latest_exposure.timing is not None
                self.latest_exposure.timing.end = datetime.datetime.now()
                self.latest_exposure.timing.mid = (
                    self.latest_exposure.timing.start
                    + (self.latest_exposure.timing.end - self.latest_exposure.timing.start) / 2
                )

                self.latest_exposure.timing.end_utc = self.latest_exposure.timing.end.astimezone(timezone.utc)
                self.latest_exposure.timing.mid_utc = self.latest_exposure.timing.mid.astimezone(timezone.utc)
                self.readout_thread = threading.Thread(
                    name=f"deepspec-camera-{self.band}-readout-thread",
                    target=self.readout,
                )
                self.readout_thread.start()

        if self.is_active(GreatEyesActivities.StoppingMeasurement):
            if not ge.DllIsBusy(addr=self.ge_device):
                self.end_activity(GreatEyesActivities.StoppingMeasurement, label=self.name)
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)
                self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            elif now - self.timings[GreatEyesActivities.StoppingMeasurement].start_time > datetime.timedelta(seconds=5):
                self.append_error(
                    f"stopping measurement takes too long ({now - self.timings[GreatEyesActivities.StoppingMeasurement].start_time} > 5 seconds)"
                )
                self.end_activity(GreatEyesActivities.StoppingMeasurement, label=self.name)
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)
                self.end_activity(GreatEyesActivities.Acquiring, label=self.name)

        if self.is_active(GreatEyesActivities.AdjustingTemperature) and self.sensor_temperature_target is not None:
            sensor_temp = self.get_sensor_temperature()
            if sensor_temp is None:
                if not ge.DllIsBusy(addr=self.ge_device):
                    self.append_error("failed reading sensor temperature")
            elif abs(sensor_temp - self.sensor_temperature_target) <= 1:
                self.end_activity(GreatEyesActivities.AdjustingTemperature, label=self._name)
                self.sensor_temperature_target = None

        if self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp):
            sensor_temp = self.get_sensor_temperature()
            if sensor_temp is None:
                if not ge.DllIsBusy(addr=self.ge_device):
                    self.append_error("failed reading sensor temperature")
            else:
                switch_temp_control_off = False
                should_power_off = False
                if (
                    self.is_active(GreatEyesActivities.CoolingDown)
                    and abs(sensor_temp - self.greateyes_settings.temp.target_cool) <= 1
                ):
                    self.end_activity(GreatEyesActivities.CoolingDown, label=self._name)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(GreatEyesActivities.StartingUp, label=self._name)
                    switch_temp_control_off = True

                if (
                    self.is_active(GreatEyesActivities.WarmingUp)
                    and abs(sensor_temp - self.greateyes_settings.temp.target_warm) <= 1
                ):
                    self.end_activity(GreatEyesActivities.WarmingUp, label=self._name)
                    if self.is_active(GreatEyesActivities.ShuttingDown):
                        self.end_activity(GreatEyesActivities.ShuttingDown, label=self._name)
                        should_power_off = True
                    switch_temp_control_off = True

                if switch_temp_control_off:
                    ret = ge.TemperatureControl_SwitchOff(addr=self.ge_device)
                    if ret:
                        self.info(f"OK: ge.TemperatureControl_SwitchOff(addr={self.ge_device})")
                    else:
                        self.error(f"could not ge.TemperatureControl_SwitchOff(addr={self.ge_device}) (ret={ret})")

                if should_power_off:
                    self.power_off()
                    self.timer.finished.set()

    def get_sensor_temperature(self) -> float | None:
        if not self.detected:
            return None

        assert self.ge_device is not None
        if ge.DllIsBusy(addr=self.ge_device):
            return None
        ret = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.ge_device)
        if ret == FAILED_TEMPERATURE:
            self.append_error(f"failed to read sensor temperature ({ret=})")
            return None
        return ret

    def get_back_temperature(self) -> float | None:
        ret = None
        if not self.detected:
            return ret

        assert self.ge_device is not None
        if not ge.DllIsBusy(addr=self.ge_device):
            ret = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.ge_device)
            if ret == FAILED_TEMPERATURE:
                self.append_error(f"failed to read back temperature ({ret=})")
                return None
            return ret

        return None

    @property
    def operational(self) -> bool:
        if not self.enabled:
            return False

        assert self.power_switch is not None
        return (
            self.power_switch.detected and self.detected
            # and not (
            #     self.is_active(GreatEyesActivities.CoolingDown)
            #     or self.is_active(GreatEyesActivities.WarmingUp)
            # )
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
            # if self.is_active(GreatEyesActivities.CoolingDown):
            #     ret.append(f"{label} camera is CoolingDown")
            # if self.is_active(GreatEyesActivities.WarmingUp):
            #     ret.append(f"{label} camera is WarmingUp")

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

    def do_execute_assignment(self, assignment: SpectrographAssignment, folder: str):
        # Runs on this band's own thread (see execute_assignment). Everything is caught and
        # logged here: an escaping exception would go to threading's excepthook, i.e. to a
        # stderr nobody reads, and Deepspec would only see the thread end.
        try:
            assert isinstance(assignment.spec.settings, DeepspecSettings)
            deepspec_settings: DeepspecSettings = assignment.spec.settings

            assert deepspec_settings.camera is not None
            greateyes_settings: GreateyesSettingsModel = deepspec_settings.camera[self.band]

            # No apply_settings() pass first: start_exposure applies this assignment's
            # settings itself, per exposure, from the same model.
            assert greateyes_settings.number_of_exposures is not None
            for exposure_number in range(1, greateyes_settings.number_of_exposures + 1):
                greateyes_settings.image_file = os.path.join(folder, f"exposure-{exposure_number:03}.fits")
                # Cleared before each exposure so a failed save cannot leave the previous
                # exposure's path here and have it moved twice.
                self.latest_saved_image_path = None
                self.start_exposure(greateyes_settings)
                while self.is_active(GreatEyesActivities.Acquiring):
                    time.sleep(0.5)

                # This exposure is finished: hand it to the mover. Per exposure rather than
                # per folder, so the local disk never holds a whole assignment. save_image()
                # already protected the write, which is what records it as a product.
                if self.latest_saved_image_path:
                    Filer().move_ram_to_shared(self.latest_saved_image_path)
                else:
                    self.error(f"exposure-{exposure_number:03} was not saved; nothing to move")
        except Exception:
            self.error(f"{function_name()}: deepspec-{self.band} assignment failed")
            logger.exception(f"{function_name()}: deepspec-{self.band} assignment failed")

    def execute_assignment(self, assignment: SpectrographAssignment, folder: str) -> threading.Thread:
        """Start this band's exposures on its own thread and return that thread.

        The caller (Deepspec.execute_assignment) joins it. It used to be discarded, leaving
        the coordinator to poll `is_working`, i.e. `is_active(Acquiring)` -- a flag this
        thread does not set until it reaches start_exposure(), several SDK calls in. The
        coordinator could therefore see every band idle and conclude the assignment was over
        before a single exposure had begun. A thread cannot be observed finished too early.
        """
        cooling_down = []
        for camera in cameras.values():
            if camera and camera.enabled and camera.is_active(GreatEyesActivities.CoolingDown):
                cooling_down.append(camera)
        if cooling_down:
            raise Exception(
                f"cannot execute assignment because the following cameras are currently cooling down: {', '.join(camera.name for camera in cooling_down)}"
            )

        thread = threading.Thread(
            name=f"deepspec-{self.band}-assignment",
            target=self.do_execute_assignment,
            args=[assignment, folder],
        )
        thread.start()
        return thread


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
    threading.Thread(name=f"make-deepspec-camera-{_band}", target=make_camera, args=[_band]).start()


if __name__ == "__main__":
    for c in cameras:
        print(cameras[c])
