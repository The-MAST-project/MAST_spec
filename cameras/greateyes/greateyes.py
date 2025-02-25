import datetime
import threading
import time
from datetime import timezone

import fastapi

from common.config import Config
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.spec import DeepspecBands
import sys
import os
import logging
from common.utils import Component, function_name, OperatingMode
from common.mast_logging import init_log
from common.networking import NetworkedDevice
from typing import List, get_args, Callable, Optional
from common.utils import RepeatTimer, BASE_SPEC_PATH
from enum import IntFlag, auto, Enum, IntEnum
from common.models.greateyes import GreateyesSettingsModel, ReadoutSpeed
from common.models.assignments import SpectrographAssignmentModel
from common.models.deepspec import DeepspecModel

import astropy.io.fits as fits
from astropy.io.fits import Card
import astropy.time as atime
from pydantic import BaseModel

sys.path.append(os.path.join(os.path.dirname(__file__), 'sdk'))
import cameras.greateyes.sdk.greateyesSDK as ge

logger = logging.getLogger('greateyes')
init_log(logger, logging.DEBUG)

dll_version = ge.GetDLLVersion()

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
    Two = 2,
    Three = 3
    Four = 4,
    
    
readout_speed_names = {
    ReadoutSpeed.ReadoutSpeed_1_MHz: '1 MHz',
    ReadoutSpeed.ReadoutSpeed_3_MHz: '3 MHz',
    ReadoutSpeed.ReadoutSpeed_500_kHz: '500 KHz',
    ReadoutSpeed.ReadoutSpeed_250_kHz: '250 KHz',
    ReadoutSpeed.ReadoutSpeed_100_kHz: '100 KHz',
    ReadoutSpeed.ReadoutSpeed_50_kHz: '50 KHz',
}


class GreatEyesActivities(IntFlag):
    CoolingDown = auto()
    WarmingUp = auto()
    Acquiring = auto()
    Exposing = auto()
    ReadingOut = auto()
    Saving = auto()
    StartingUp = auto()
    ShuttingDown = auto()
    SettingParameters = auto()
    Probing = auto()



class ExposureTiming:
    start: datetime.datetime
    start_utc: datetime.datetime

    mid: datetime.datetime
    mid_utc: datetime.datetime

    end: datetime.datetime
    end_utc: datetime.datetime

    duration: datetime.timedelta


class Exposure:
    settings: GreateyesSettingsModel
    timing: ExposureTiming

    def __init__(self):
        self.timing = ExposureTiming()
        self.timing.start = datetime.datetime.now()
        self.timing.start_utc = self.timing.start.astimezone(timezone.utc)


class GreatEyes(SwitchedOutlet, NetworkedDevice, Component):

    def __init__(self, band: DeepspecBands):
        self._initialized = False
        self._detected = False
        self._connected = False
        Component.__init__(self)

        self.band = band
        self.conf = Config().get_specs()['deepspec'][self.band]    # specific to this camera instance
        self.settings: GreateyesSettingsModel = GreateyesSettingsModel(**self.conf['settings'])
        self.latest_settings: Optional[GreateyesSettingsModel] = None
        self.ge_device = self.conf['device']
        self._name = f"Deepspec-{self.band}"
        self.outlet_name = f"Deepspec{self.band}"
        self.errors = []

        NetworkedDevice.__init__(self, self.conf)
        SwitchedOutlet.__init__(self, outlet_name=f'{self.outlet_name}', domain=OutletDomain.Spec)

        self.settings: GreateyesSettingsModel = GreateyesSettingsModel(**self.conf['settings'])

        self.enabled = bool(self.conf['enabled']) if 'enabled' in self.conf else False

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
            self.error(f"camera {self.name} is disabled")
            return

        self.last_probe_time = None
        self.timer = RepeatTimer(1, function=self.on_timer)
        self.timer.name = f'deepspec-camera-{self.band}-timer-thread'
        self.timer.start()

    def try_connect_camera(self):
        #
        # Clean-up previous connections, if existent
        # NOTE: these actions may return False, but that seems OK
        #
        ret = ge.DisconnectCamera(addr=self.ge_device)
        # self.debug(f"ge.DisconnectCamera(addr={self.ge_device}) -> {ret}")
        try:
            ret = ge.DisconnectCameraServer(addr=self.ge_device)
            self.debug(f"ge.DisconnectCameraServer(addr={self.ge_device}) -> {ret}")
        except Exception as e:
            self.error(f"ge.DisconnectCameraServer(addr={self.ge_device}) caught error {e}")
            return

        # This just tells the Greateyes server how to interface with the specific camera
        # NOTE: it should not fail
        ret = ge.SetupCameraInterface(ge.connectionType_Ethernet, ipAddress=self.network.ipaddr, addr=self.ge_device)
        if not ret:
            self.error(f"could not ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                              f"ipaddress={self.network.ipaddr}, addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")
            self.end_activity(GreatEyesActivities.Probing)
            return
        self.debug(f"OK: ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                          f"ipaddress={self.network.ipaddr}, addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")

        ret = ge.ConnectToSingleCameraServer(addr=self.ge_device)
        if not ret:
            self.error(f"could not ge.ConnectToSingleCameraServer(addr={self.ge_device}) " +
                              f"(ret={ret}, msg='{ge.StatusMSG}')")
            self.end_activity(GreatEyesActivities.Probing)
            return
        self.debug(f"OK: ge.ConnectToSingleCameraServer(addr={self.ge_device}) " +
                          f"(ret={ret}, msg='{ge.StatusMSG}')")

        model = []
        ret = ge.ConnectCamera(model=model, addr=self.ge_device)
        if not ret:
            self.error(f"could not ge.ConnectCamera(model=[], addr={self.ge_device}) (ret={ret}, " +
                              f"msg='{ge.StatusMSG}')")
            self.end_activity(GreatEyesActivities.Probing)
            return
        self.debug(f"OK: ge.ConnectCamera(model=[], addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")

        self.model_id = model[0]
        self.model = model[1]

        self._connected = True
        self._detected = True

    def probe(self):
        """
        Tries to detect the camera
        """
        if not self.power_switch.detected:
            return

        if not self.enabled or self.detected:
            return
        
        self.start_activity(GreatEyesActivities.Probing, label=self.name)
        self.try_connect_camera()

        default_settings = GreateyesSettingsModel(**self.conf['settings'])
        if not self.detected:
            if self.is_off():
                self.power_on()
            else:
                self.cycle()
            boot_delay = default_settings.probing.boot_delay
            self.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
            time.sleep(boot_delay)

            self.try_connect_camera()
            if not self.detected:
                self.end_activity(GreatEyesActivities.Probing)
                return

        self.firmware_version = ge.GetFirmwareVersion(addr=self.ge_device)

        ret = ge.InitCamera(addr=self.ge_device)
        if not ret:
            self.error(f"FAILED - ge.InitCamera(addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")
            ge.DisconnectCamera(addr=self.ge_device)
            ge.DisconnectCameraServer(addr=self.ge_device)
            self.end_activity(GreatEyesActivities.Probing)
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

        self.info(f"greateyes: ipaddr='{self.network.ipaddr}', size={self.x_size}x{self.y_size}, " +
                  f"model_id={self.model_id}, model='{self.model}, fw_version={self.firmware_version}")

        self.apply_settings(default_settings)

        self.set_led(False)
        self.end_activity(GreatEyesActivities.Probing)

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

        ret = ge.SetLEDStatus(on_off, addr=self.ge_device)
        if not ret:
            self.error(f"could not set back LED to {'ON' if on_off else 'OFF'}")

    def __repr__(self):
        return (f"<Greateyes>(band={self.band}, id={self.band}, address='{self.network.ipaddr}', model='{self.model}', " +
                f"model_id='{self.model_id}', firmware_version={self.firmware_version})")

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

    def status(self) -> dict:
        ret = {
            'band': self.band,
            'ipaddr': self.network.ipaddr,
            'detected': self.detected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'enabled': self.enabled,
            'activities': self.activities,
            'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
        }
        if self.enabled and self.detected:
            ret |= {
             'powered': self.is_on(),
             'connected': self.connected,
             'addr': self.ge_device,
             'idle': self.is_idle(),
             'min_temp': self.min_temp,
             'max_temp': self.max_temp,
             'front_temperature': ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.ge_device),
             'back_temperature': ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.ge_device),
             'errors': self.errors,
             'latest_exposure': self.latest_exposure,
             'latest_settings': self.latest_settings,
            }
        return ret

    def cool_down(self):
        if not self.detected:
            return

        if ge.TemperatureControl_SetTemperature(temperature=self.settings.temp.target_cool, addr=self.ge_device):
            self.start_activity(GreatEyesActivities.CoolingDown, label=self.name)

    def warm_up(self):
        if not self.detected:
            return
        if ge.TemperatureControl_SetTemperature(temperature=self.max_temp, addr=self.ge_device):
            self.start_activity(GreatEyesActivities.WarmingUp, label=self.name)

    def startup(self):
        if not self.detected:
            return
        if OperatingMode().production:
            self.start_activity(GreatEyesActivities.StartingUp, label=self.name)
            self.cool_down()
        else:
            self.info("MAST_DEBUG is set, not cooling down on startup")
        self._was_shut_down = False

    def shutdown(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.ShuttingDown, label=self.name)
        if self.is_active(GreatEyesActivities.Exposing):
            self.abort()
        if OperatingMode().production:
            self.warm_up()
        else:
            self.info("MAST_DEBUG is set, not warming up on shutdown")
        self._was_shut_down = True

    def _apply_setting(self, func: Callable, arg):
        op = f"{func.__name__ if hasattr(func, '__name__') else str(func)}({arg}, addr={self.ge_device})"
        ret = func(*arg, addr=self.ge_device) if isinstance(arg, (tuple, list)) else func(arg, addr=self.ge_device)
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
            self.errors.append(f"not detected")
            return

        print("apply_settings:\n" + settings.model_dump_json(indent=2))
        self.start_activity(GreatEyesActivities.SettingParameters)
        self._apply_setting(ge.SetBitDepth, settings.bytes_per_pixel)
        self._apply_setting(ge.SetupSensorOutputMode, settings.readout.mode.value)
        self._apply_setting(ge.SetReadOutSpeed, settings.readout.speed.value)
        self._apply_setting(ge.SetBinningMode, (settings.binning.x, settings.binning.y))
        if settings.crop.enabled:
            self._apply_setting(ge.SetupCropMode2D, (settings.crop.col, settings.crop.line))
            self._apply_setting(ge.ActivateCropMode, True)
        else:
            self._apply_setting(ge.ActivateCropMode, False)
        if settings.shutter.automatic:
            self._apply_setting(ge.SetShutterTimings, (settings.shutter.open_time, settings.shutter.close_time))

        self.latest_settings = settings
        self.end_activity(GreatEyesActivities.SettingParameters)

    def expose(self, settings: GreateyesSettingsModel):

        self.errors = []
        if not self.detected:
            self.errors.append("not detected")
            return

        if not self.is_idle():
            if ge.DllIsBusy(addr=self.ge_device):
                self.append_error("could not start exposure: ge.DllIsBusy()")
                return

            if self.is_active(GreatEyesActivities.CoolingDown):
                ret = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.ge_device)
                if ret == FAILED_TEMPERATURE:
                    self.append_error(f"could not read sensor temperature ({ret=})")
                else:
                    delta_temp = abs(self.settings.temp.target_cool - ret)
                    self.append_error(f"cannot expose while cooling down ({delta_temp=} to cool)")
                return

            if not self.is_idle():
                self.append_error(f"camera is active ({self.activities=})")
                return

        self.latest_exposure.settings = settings

        self.start_activity(GreatEyesActivities.Acquiring)
        self._apply_setting(ge.SetupSensorOutputMode, self.latest_settings.readout.mode)
        if not self._apply_setting(ge.SetExposure, int(self.latest_exposure.settings.exposure_duration * 1000)):
            self.end_activity(GreatEyesActivities.Acquiring)
            return

        mode = 2 if self.latest_settings.shutter.automatic else 1
        self._apply_setting(ge.OpenShutter, mode)

        ret = ge.StartMeasurement_DynBitDepth(addr=self.ge_device, showShutter=self.settings.shutter.automatic)
        if ret:
            self.start_activity(GreatEyesActivities.Exposing)
            self.latest_exposure.timing.start_utc = datetime.datetime.now(datetime.UTC)
            self.latest_exposure.timing.start = datetime.datetime.now()
        else:
            self.append_error(f"could not ge.StartMeasurement_DynBitDepth(addr={self.ge_device}) ({ret=})")

    def readout(self):
        if not self.detected:
            self.end_activity(GreatEyesActivities.Acquiring)
            return

        if not self.latest_exposure.settings.image_file:
            self.end_activity(GreatEyesActivities.Acquiring)
            raise Exception(f"empty image_file")

        self.start_activity(GreatEyesActivities.ReadingOut)
        image_array = ge.GetMeasurementData_DynBitDepth(addr=self.ge_device)
        self.end_activity(GreatEyesActivities.ReadingOut)

        if not self.latest_settings.shutter.automatic:
            ret = ge.OpenShutter(0, addr=self.ge_device)
            if not ret:
                self.append_error(f"could not close shutter with ge.OpenShutter(0, addr={self.ge_device})")

        self.start_activity(GreatEyesActivities.Saving)
        hdr = fits.Header()
        hdr.append(Card('INSTRUME', 'DEEPSPEC', 'Instrument'))
        hdr.append(Card('TELESCOP', 'WAO-MAST', 'Telescope'))
        hdr.append(Card('DETECTOR', 'DEEPSPEC', 'Detector'))
        hdr.append(Card('BAND', f'DeepSpec-{self.band}', 'DEEPSPEC BAND'))
        hdr.append(Card('CAM_IP', self.network.ipaddr, 'Camera IP address'))
        hdr.append(Card('TYPE', 'RAW', 'Exposure type'))
        
        hdr.append(Card('LT_START', self.latest_exposure.timing.start.strftime(FITS_DATE_FORMAT), 'Exposure time start (local)'))
        hdr.append(Card('LT_MID', self.latest_exposure.timing.mid.strftime(FITS_DATE_FORMAT), 'Exposure mid time (local)'))
        hdr.append(Card('LT_END', self.latest_exposure.timing.end.strftime(FITS_DATE_FORMAT), 'Exposure end time (local)'))
        
        hdr.append(Card('T_START', self.latest_exposure.timing.start_utc.strftime(FITS_DATE_FORMAT), 'Exposure time start (UTC)'))
        hdr.append(Card('T_MID', self.latest_exposure.timing.mid_utc.strftime(FITS_DATE_FORMAT), 'Exposure mid time (UTC)'))
        hdr.append(Card('T_END', self.latest_exposure.timing.end_utc.strftime(FITS_DATE_FORMAT), 'Exposure end time (UTC)'))
        
        hdr.append(Card('T_EXP', self.latest_exposure.settings.exposure_duration, 'TOTAL INTEGRATION TIME'))
        hdr.append(Card('TEMPGOAL', self.settings.temp.target_cool, 'GOAL DETECTOR TEMPERATURE'))
        hdr.append(Card('TEMPFLAG', self.backside_temp_safe, 'DETECTOR BACKSIDE TEMPERATURE SAFETY FLAG'))
        hdr.append(Card('DATE-OBS', self.latest_exposure.timing.mid_utc.strftime(FITS_DATE_FORMAT), 'OBSERVATION DATE'))
        # hdr.append(Card('MJD-OBS', atime.Time(self.latest_exposure.timing.mid_utc).strftime(FITS_DATE_FORMAT), 'MJD OF OBSERVATION MIDPOINT'))
        hdr.append(Card('RDSPEED', readout_speed_names[self.latest_exposure.settings.readout.speed], 'PIXEL READOUT FREQUENCY'))
        hdr.append(Card('CDELT1', self.latest_exposure.settings.binning.x, 'BINNING IN THE X DIRECTION'))
        hdr.append(Card('CDELT2', self.latest_exposure.settings.binning.y, 'BINNING IN THE Y DIRECTION'))
        hdr.append(Card('NAXIS', 2, 'NUMBER OF AXES IN FRAME'))
        hdr.append(Card('NAXIS1', self.x_size / self.latest_exposure.settings.binning.x, 'NUMBER OF PIXELS IN THE X DIRECTION'))
        hdr.append(Card('NAXIS2', self.y_size / self.latest_exposure.settings.binning.y, 'NUMBER OF PIXELS IN THE Y DIRECTION'))
        hdr.append(Card('PIXSIZE', self.pixel_size_microns, 'PIXEL SIZE IN MICRONS'))
        hdr.append(Card('BITPIX', self.latest_settings.bytes_per_pixel, '# of bits storing pix values'))
        hdu = fits.PrimaryHDU(image_array, header=hdr)
        hdul = fits.HDUList([hdu])

        filename = self.latest_exposure.settings.image_file
        if not filename.endswith('.fits'):
            filename += '.fits'
        try:
            self.start_activity(GreatEyesActivities.Saving)
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            hdul.writeto(filename)
            self.end_activity(GreatEyesActivities.Saving)
            self.info(f"saved exposure to '{filename}'")
        except Exception as e:
            self.end_activity(GreatEyesActivities.Acquiring)
            self.debug(f"failed to save exposure (error: {e})")
        self.end_activity(GreatEyesActivities.Acquiring)

    @property
    def is_working(self) -> bool:
        return self.is_active(GreatEyesActivities.Acquiring)

    def abort(self):
        if not self.detected:
            return

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

        if (not self.is_active(GreatEyesActivities.Probing) and
                not self.detected and
                (self.last_probe_time is None or
                 datetime.datetime.now() - self.last_probe_time > datetime.timedelta(seconds=self.settings.probing.interval))):
            self.last_probe_time = datetime.datetime.now()
            self.probe()
            return

        if not self.detected:
            return

        now = datetime.datetime.now()
        if (self.last_backside_temp_check is None or (now - self.last_backside_temp_check) >
                datetime.timedelta(seconds=self.settings.temp.check_interval)):

            ret = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.ge_device)
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
                self.end_activity(GreatEyesActivities.Exposing)
                
                self.latest_exposure.timing.end = datetime.datetime.now()
                self.latest_exposure.timing.mid = (self.latest_exposure.timing.start +
                                              (self.latest_exposure.timing.end - self.latest_exposure.timing.start) / 2)
                
                self.latest_exposure.timing.end_utc = self.latest_exposure.timing.end.astimezone(timezone.utc)
                self.latest_exposure.timing.mid_utc = self.latest_exposure.timing.mid.astimezone(timezone.utc)
                self.readout_thread = threading.Thread(name=f'deepspec-camera-{self.band}-readout-thread',
                                                       target=self.readout)
                self.readout_thread.start()
            else:
                elapsed = (now - self.timings[GreatEyesActivities.Exposing].start_time).seconds
                max_expected = self.latest_exposure.settings.exposure_duration * 2
                if elapsed > max_expected:
                    self.append_error(f"exposure takes too long ({elapsed=} > {max_expected=})")
                    ret = ge.StopMeasurement(addr=self.ge_device)
                    if ret:
                        self.append_error(f"could not ge.StopMeasurement(addr={self.ge_device}) ({ret=})")
                    else:
                        self.end_activity(GreatEyesActivities.Exposing)

        if self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp):
            front_temp = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.ge_device)
            if front_temp == FAILED_TEMPERATURE:
                self.append_error(f"failed reading sensor temperature")
            else:
                switch_temp_control_off = False
                should_power_off = False
                if self.is_active(GreatEyesActivities.CoolingDown) and abs(front_temp - self.settings.temp.target_cool) <= 1:
                    self.end_activity(GreatEyesActivities.CoolingDown, label=self.name)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(GreatEyesActivities.StartingUp, label=self.name)
                    switch_temp_control_off = True

                if self.is_active(GreatEyesActivities.WarmingUp) and abs(front_temp >= self.settings.temp.target_warm) <= 1:
                    self.end_activity(GreatEyesActivities.WarmingUp, label=self.name)
                    if self.is_active(GreatEyesActivities.ShuttingDown):
                        self.end_activity(GreatEyesActivities.ShuttingDown, label=self.name)
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

    @property
    def operational(self) -> bool:
        return (self.power_switch.detected and
                self.detected and not
                (self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp)))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = f"{self.name}:"
        if not self.power_switch.detected:
            ret.append(f"{label} {self.power_switch} not detected")
        elif self.is_off():
            ret.append(f"{label} {self.power_switch}:{self.outlet_name} is OFF")
        else:
            if not self.detected:
                ret.append(f"{label} camera (at {self.network.ipaddr}) not detected")
            if self.is_active(GreatEyesActivities.CoolingDown):
                ret.append(f'{label} camera is CoolingDown')
            if self.is_active(GreatEyesActivities.WarmingUp):
                ret.append(f'{label} camera is WarmingUp')

        return ret
    
    def error(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self.name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self.name}: ",)
        
        logger.error(*args, **kwargs)
        
    def info(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self.name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self.name}: ",)
        
        logger.info(*args, **kwargs)

    def debug(self, *args, **kwargs):
        # Prepend self.name to the message
        if args:
            message = f"{self.name}: {args[0]}"
            args = (message,) + args[1:]
        else:
            args = (f"{self.name}: ",)

        logger.debug(*args, **kwargs)

    def do_execute_assignment(self, assignment: SpectrographAssignmentModel, folder: str):
        deepspec_assignment: DeepspecModel = assignment.spec
        settings: GreateyesSettingsModel = deepspec_assignment.camera[self.band]

        self.apply_settings(settings=settings)

        for exposure_number in range(1, settings.number_of_exposures+1):
            settings.image_file = os.path.join(folder, f"exposure-{exposure_number:03}.fits")
            self.expose(settings)
            while self.is_active(GreatEyesActivities.Acquiring):
                time.sleep(.5)

    def execute_assignment(self,
                           assignment: SpectrographAssignmentModel,
                           folder: str):
        threading.Thread(target=self.do_execute_assignment, args=[assignment, folder]).start()


class GreateyesFactory:
    _instances = {
        'I': None,
        'G': None,
        'R': None,
        'U': None,
    }

    @classmethod
    def get_instance(cls, band: DeepspecBands) -> GreatEyes:
        if not cls._instances[band]:
            cls._instances[band] = GreatEyes(band=band)
        return cls._instances[band]

#
# FastAPI
#

def camera_expose(band: DeepspecBands, seconds: float):

    camera = [cam for cam in cameras if cam.band == band][0]
    if not camera.detected:
        return

    threading.Thread(
        name=f"camera-{camera.band}-exposure-{seconds}sec",
        target=camera.acquire,
        args=[seconds]
    ).start()

def make_camera(b: DeepspecBands):
    op = function_name()
    try:
        cameras[b] = GreateyesFactory.get_instance(band=b)
    except Exception as e:
        logger.error(f"{op}: caught {e}")
        cameras[b] = None

cameras = {}
for _band in get_args(DeepspecBands):
    threading.Thread(
        name=f"make-deepspec-camera-{_band}",
        target=make_camera,
        args=[_band]
    ).start()

base_path = BASE_SPEC_PATH + 'deepspec/camera/'
tag = 'Deepspec Cameras'
router = fastapi.APIRouter()

# router.add_api_route(base_path + 'set_camera_params', tags=[tag], endpoint=set_camera_params)
router.add_api_route(base_path + 'camera_expose', tags=[tag], endpoint=camera_expose)

if __name__ == "__main__":
    for c in cameras:
        print(c)
        c.power_off()
