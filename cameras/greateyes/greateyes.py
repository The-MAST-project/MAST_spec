import datetime
import threading
import time
from datetime import timezone

import fastapi

from common.config import Config
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.spec import SpecExposureSettings, DeepspecBands
from common.filer import Filer
import sys
import os
import logging
from common.utils import Component, function_name
from common.mast_logging import init_log
from common.networking import NetworkedDevice
from typing import List, get_args, Callable
from common.utils import RepeatTimer, BASE_SPEC_PATH
from enum import IntFlag, auto, Enum, IntEnum
from common.models.greateyes import GreateyesSettingsModel, ReadoutSpeed
from pydantic.v1.utils import deep_update

import astropy.io.fits as fits
import astropy.time as atime
from fits import FITS_HEADER_COMMENTS, FITS_STANDARD_FIELDS
from pydantic import BaseModel

sys.path.append(os.path.join(os.path.dirname(__file__), 'sdk'))
import cameras.greateyes.sdk.greateyesSDK as ge

logger = logging.getLogger('greateyes')
init_log(logger, logging.DEBUG)

dll_version = ge.GetDLLVersion()

FAILED_TEMPERATURE = -300


# class ReadoutAmplifiers(IntEnum):
#     OSR = 0,
#     OSL = 1,
#     OSR_AND_OSL = 2,


# class Gain(IntEnum):
#     Low = 0,    # Low ( Max. Dyn. Range )
#     High = 1,   # Std ( High Sensitivity )
#
#
# class GainSettingModel(BaseModel):
#     gain: Gain

    # # Validator to parse int into Gains enum
    # @model_validator(mode="before")
    # @classmethod
    # def parse_gain(cls, values):
    #     print(f"parse_gain: values: {values}")
    #     if isinstance(values, dict) and 'gain' in values:
    #         v = values['gain']
    #         if isinstance(v, int):
    #             try:
    #                 values['gain'] = Gains(v)
    #             except ValueError:
    #                 raise ValueError(f"Invalid integer for Gains: {v}")
    #         elif isinstance(v, str):
    #             try:
    #                 values['gain'] = Gains[v.capitalize()]
    #             except KeyError:
    #                 raise ValueError(f"Invalid string for Gains: {v}")
    #     return values


class CropSettingsModel(BaseModel):
    col: int
    line: int
    enabled: bool


class Band(str, Enum):
    U = 'U',
    G = 'G',
    R = 'R',
    I = 'I',


class BytesPerPixel(IntEnum):
    Two = 2,
    Three = 3
    Four = 4,


bytes_per_pixels = Enum('BytesPerPixel', list(zip(
    list(BytesPerPixel.__members__), list(BytesPerPixel.__members__))))
    
    
class ColumnsBinning(IntEnum):     # X binning
    NoBinning = 0,
    
    
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
        self.latest_settings: GreateyesSettingsModel | None = None
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

    def probe(self):
        """
        Tries to detect the camera
        """
        if not self.power_switch.detected:
            return

        self.start_activity(GreatEyesActivities.Probing)
        if self.is_off():
            self.power_on()
        else:
            self.cycle()

        boot_delay = self.conf['boot_delay'] if 'boot_delay' in self.conf else 20
        self.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
        time.sleep(boot_delay)

        #
        # Clean-up previous connections, if existent
        # NOTE: these actions may return False, but that seems OK
        #
        ret = ge.DisconnectCamera(addr=self.ge_device)
        # self.debug(f"ge.DisconnectCamera(addr={self.ge_device}) -> {ret}")
        try:
            ret = ge.DisconnectCameraServer(addr=self.ge_device)
            # self.debug(f"ge.DisconnectCameraServer(addr={self.ge_device}) -> {ret}")
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
        self._connected = True
        self._detected = True

        self.model_id = model[0]
        self.model = model[1]

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

        ret = ge.SetBitDepth(self.settings.bytes_per_pixel, addr=self.ge_device)
        if not ret:
            self.error(f"FAILED - ge.SetBitDepth({self.settings.bytes_per_pixel}, addr={self.ge_device}) ({ret=})")
        self.info(f"OK - ge.SetBitDepth({self.settings.bytes_per_pixel}, addr={self.ge_device})")

        self.info(f"greateyes: ipaddr='{self.network.ipaddr}', fw_version={self.firmware_version}, size={self.x_size}x{self.y_size}, model_id={self.model_id}, model='{self.model}")

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
            self.start_activity(GreatEyesActivities.CoolingDown)

    def warm_up(self):
        if not self.detected:
            return
        if ge.TemperatureControl_SetTemperature(temperature=self.max_temp, addr=self.ge_device):
            self.start_activity(GreatEyesActivities.WarmingUp)

    def startup(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.StartingUp)
        self.cool_down()
        self._was_shut_down = False

    def shutdown(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.ShuttingDown)
        if self.is_active(GreatEyesActivities.Exposing):
            self.abort()
        self.warm_up()
        self._was_shut_down = True

    def _apply_setting(self, func: Callable, arg):
        op = f"{func.__name__ if hasattr(func, '__name__') else str(func)}({arg})"
        ret = func(*arg, addr=self.ge_device) if isinstance(arg, (tuple, list)) else func(arg)
        if ret:
            self.info(f"OK - {op}")
        else:
            self.append_error(f"FAILED - {op}")

    def apply_settings(self, new_settings: GreateyesSettingsModel):
        """
        Enforces settings onto this specific camera.
        * The camera has default settings (from configuration)
        * The default settings are updated with the supplied settings
        * The resulting (combined) settings are applied to the camera
        :param new_settings: e.g. from an assignment
        :return:
        """

        self.errors = []
        if not self.detected:
            self.errors.append(f"not detected")
            return

        d = deep_update(self.settings.model_dump(), new_settings.model_dump())
        settings: GreateyesSettingsModel = GreateyesSettingsModel(**d)

        self.start_activity(GreatEyesActivities.SettingParameters)
        self._apply_setting(ge.SetupSensorOutputMode, settings.readout.amplifiers)
        self._apply_setting(ge.SetBinningMode, (settings.binning.x, settings.binning.y))
        self._apply_setting(ge.SetupGain, settings.gain)
        self._apply_setting(ge.SetBitDepth, settings.bytes_per_pixel)
        self._apply_setting(ge.SetReadOutSpeed, settings.readout.speed)
        if settings.crop.enabled:
            self._apply_setting(ge.SetupCropMode2D, (settings.crop.col, settings.crop.line))
            self._apply_setting(ge.ActivateCropMode, True)
        else:
            self._apply_setting(ge.ActivateCropMode, False)
        if settings.shutter.automatic:
            self._apply_setting(ge.SetShutterTimings, (settings.shutter.open_time, settings.shutter.close_time))

        self.latest_settings = settings
        self.end_activity(GreatEyesActivities.SettingParameters)

        # op = f"OK - ge.SetBinningMode({settings.binning.x=}, {settings.binning.y=})"
        # ret = ge.SetBinningMode(settings.binning.x, settings.binning.y, addr=self.ge_device)
        # if ret:
        #     self.info(f"OK - {op}")
        # else:
        #     self.append_error(f"FAILED - {op} ({ret=})")

        # op = f"ge.SetupGain({settings.gain})"
        # ret = ge.SetupGain(settings.gain, addr=self.ge_device)
        # if ret:
        #     self.info(f"OK - {op}")
        # else:
        #     self.append_error(f"FAILED - {op} ({ret=})")

        # op = f"ge.SetBitDepth({settings.bytes_per_pixel})"
        # ret = ge.SetBitDepth(settings.bytes_per_pixel, addr=self.ge_device)
        # if ret:
        #     self.info(f"OK - {op}")
        # else:
        #     self.append_error(f"FAILED - {op} ({ret=})")

        # f"ge.SetReadOutSpeed({settings.readout.speed})"
        # ret = ge.SetReadOutSpeed(settings.readout.speed, addr=self.ge_device)
        # if ret:
        #     self.info(f"OK - {op}")
        # else:
        #     self.append_error(f"FAILED - {op} ({ret=})")

        # self.end_activity(GreatEyesActivities.SettingParameters)
        # if save:
        #     pass

    def expose(self, settings: SpecExposureSettings):
        # TODO: get acquisition folder as parameter

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

        if settings.duration is None:
            settings.duration = self.conf['exposure'] if 'exposure' in self.conf else None
        if settings.duration is None:
            raise Exception(f"cannot figure out exposure time")
        self.latest_exposure.settings = settings

        ret = ge.SetExposure(self.latest_exposure.settings.exposure_duration, addr=self.ge_device)
        if not ret:
            self.append_error(f"could not ge.SetExposure({settings.duration=}, addr={self.ge_device}) ({ret=})")
            return

        shutter_state = 1       # open
        if self.latest_settings.shutter.automatic:
            shutter_state = 2   # automatic
        ret = ge.OpenShutter(2, addr=self.ge_device)
        if not ret:
            self.append_error(f"could not open shutter with ge.OpenShutter({shutter_state})")
            return

        ret = ge.StartMeasurement_DynBitDepth(addr=self.ge_device)
        if ret:
            self.start_activity(GreatEyesActivities.Exposing)
            self.latest_exposure.timing.start_utc = datetime.datetime.now(datetime.UTC)
            self.latest_exposure.timing.start = datetime.datetime.now()
        else:
            self.append_error(f"could not ge.StartMeasurement_DynBitDepth(addr={self.ge_device}) ({ret=})")

    def readout(self):
        if not self.detected:
            return

        if not self.latest_exposure.settings.output_folder:
            pass # make a folder?

        self.start_activity(GreatEyesActivities.ReadingOut)
        image_array = ge.GetMeasurementData_DynBitDepth(addr=self.ge_device)
        self.end_activity(GreatEyesActivities.ReadingOut)

        if not self.latest_settings.shutter.automatic:
            ret = ge.OpenShutter(0, addr=self.ge_device)
            if not ret:
                self.append_error(f"could not close shutter with ge.OpenShutter(0, addr={self.ge_device})")

        self.start_activity(GreatEyesActivities.Saving)
        hdr = {}
        for key in FITS_STANDARD_FIELDS.keys():
            hdr[key] = FITS_STANDARD_FIELDS[key]
        hdr['BAND'] = 'DeepSpec_' + self.band
        hdr['CAMERA_IP'] = self.conf['ipaddr']
        hdr['TYPE'] = 'RAW'
        
        hdr['LOCAL_T_START'] = f"{self.latest_exposure.timing.start:FITS_DATE_FORMAT}"
        hdr['LOCAL_T_MID'] = f"{self.latest_exposure.timing.mid:FITS_DATE_FORMAT}"
        hdr['LOCAL_T_END'] = f"{self.latest_exposure.timing.end:FITS_DATE_FORMAT}"
        
        hdr['T_START'] = f"{self.latest_exposure.timing.start_utc:FITS_DATE_FORMAT}"
        hdr['T_MID'] = f"{self.latest_exposure.timing.mid_utc:FITS_DATE_FORMAT}"
        hdr['T_END'] = f"{self.latest_exposure.timing.end_utc:FITS_DATE_FORMAT}"
        
        hdr['T_EXP'] = self.latest_exposure.timing.duration
        hdr['TEMP_GOAL'] = self.settings.temp.target_cool
        hdr['TEMP_SAFE_FLAG'] = self.backside_temp_safe
        hdr['DATE-OBS'] = hdr['T_MID']
        hdr['MJD-OBS'] = atime.Time(self.latest_exposure.timing.mid_utc).mjd
        hdr['READOUT_SPEED'] = readout_speed_names[self.latest_exposure.settings.readout.speed]
        hdr['CDELT1'] = self.latest_exposure.settings.binning.x
        hdr['CDELT2'] = self.latest_exposure.settings.binning.y
        hdr['NAXIS'] = 2
        hdr['NAXIS1'] = self.x_size
        hdr['NAXIS2'] = self.y_size
        hdr['PIXEL_SIZE'] = self.pixel_size_microns
        hdr['BITPIX'] = self.latest_settings.bytes_per_pixel
        for key in list(hdr.keys()):
            hdr[key] = (hdr[key], FITS_HEADER_COMMENTS[key])
        header = fits.Header()
        for key in hdr.keys():
            header[key] = hdr[key]
        hdu = fits.PrimaryHDU(image_array, header=header)
        hdul = fits.HDUList([hdu])

        filename = os.path.join(Filer().ram.root, self.latest_exposure.settings.output_folder, self.name)
        if self.latest_exposure.settings.number_in_sequence:
            filename += f"_{self.latest_exposure.settings.number_in_sequence}"
        filename += '.fits'
        try:
            self.start_activity(GreatEyesActivities.Saving)
            hdul.writeto(filename)
            self.end_activity(GreatEyesActivities.Saving)
            Filer().move_ram_to_shared(filename)
            self.info(f"saved exposure to '{filename}'")
        except Exception as e:
            self.debug(f"failed to save exposure (error: {e})")

    @property
    def is_working(self) -> bool:
        return (self.is_active(GreatEyesActivities.Exposing) or
                self.is_active(GreatEyesActivities.ReadingOut) or
                self.is_active(GreatEyesActivities.Saving))

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

        if (not self.is_active(GreatEyesActivities.Probing) and
                not self.detected and
                (self.last_probe_time is None or
                 datetime.datetime.now() - self.last_probe_time > datetime.timedelta(seconds=self.settings.probe_interval))):
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
                elapsed = now - self.timings[GreatEyesActivities.Exposing].start_time
                max_expected = self.latest_exposure.settings.exposure + 10
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
                    self.end_activity(GreatEyesActivities.CoolingDown)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(GreatEyesActivities.StartingUp)
                    switch_temp_control_off = True

                if self.is_active(GreatEyesActivities.WarmingUp) and abs(front_temp >= self.settings.temp.target_warm) <= 1:
                    self.end_activity(GreatEyesActivities.WarmingUp)
                    if self.is_active(GreatEyesActivities.ShuttingDown):
                        self.end_activity(GreatEyesActivities.ShuttingDown)
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

    # def perform_task(self, task: Task):
    #     pass


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
