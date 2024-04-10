import datetime
import threading
import time

import fastapi
from fastapi import Query

from dlipower.dlipower.dlipower import SwitchedPowerDevice
from config import Config
import sys
import os
import logging
from utils import init_log, PathMaker, Component
from networking import NetworkedDevice
from typing import List
from copy import deepcopy
from utils import RepeatTimer, BASE_SPEC_PATH
from enum import IntFlag, auto, Enum
from datetime import timedelta, datetime

import astropy.io.fits as fits
import astropy.time as atime
from fits import FITS_HEADER_COMMENTS, FITS_STANDARD_FIELDS

sys.path.append(os.path.join(os.path.dirname(__file__), 'sdk'))
import greateyesSDK as ge

logger = logging.getLogger('greateyes')
init_log(logger, logging.DEBUG)

dll_version = ge.GetDLLVersion()

FAILED_TEMPERATURE = -300


class ReadoutAmplifiers(Enum):
    OSR = 0,
    OSL = 1,
    OSR_AND_OSL = 2,


readout_amplifiers = Enum('ReadoutAmplifiers', list(zip(
    list(ReadoutAmplifiers.__members__), list(ReadoutAmplifiers.__members__))))


class Gain(Enum):
    Low = 0,    # Low ( Max. Dyn. Range )
    High = 1,   # Std ( High Sensitivity )


gains = Enum('Gains', list(zip(
    list(Gain.__members__), list(Gain.__members__))))


class Band(str, Enum):
    U = 'U',
    G = 'G',
    R = 'R',
    I = 'I',


class ReadoutSpeed(Enum):
    KHz_50 = ge.readoutSpeed_50_kHz
    KHz_100 = ge.readoutSpeed_100_kHz
    KHz_250 = ge.readoutSpeed_250_kHz
    KHz_500 = ge.readoutSpeed_500_kHz
    MHz_1 = ge.readoutSpeed_1_MHz
    MHz_3 = ge.readoutSpeed_3_MHz


readout_speeds = Enum('ReadoutSpeeds', list(zip(
    list(ReadoutSpeed.__members__), list(ReadoutSpeed.__members__))))


class Binning(Enum):   # Y binning
    NoBinning = 0,
    Two = 1,
    Four = 2,
    Eight = 3,
    Sixteen = 4,
    ThirtyTwo = 5,
    SixtyFour = 6,
    OneHundredTwentyEight = 7,
    Full = 8,


binnings = Enum('Binnings', list(zip(
    list(Binning.__members__), list(Binning.__members__))))


class BytesPerPixel(Enum):
    Two = 2,
    Three = 3
    Four = 4,


bytes_per_pixels = Enum('BytesPerPixel', list(zip(
    list(BytesPerPixel.__members__), list(BytesPerPixel.__members__))))
    
    
class ColumnsBinning(Enum):     # X binning
    NoBinning = 0,
    
    
readout_speed_names = {
    ReadoutSpeed.MHz_1:       '1 MHz',
    ReadoutSpeed.MHz_3:       '3 MHz',
    ReadoutSpeed.KHz_500:   '500 KHz',
    ReadoutSpeed.KHz_250:   '250 KHz',
    ReadoutSpeed.KHz_100:   '100 KHz',
    ReadoutSpeed.KHz_50:     '50 KHz',
}

defaults = {
    'readout-amplifiers': ReadoutAmplifiers.OSR,
    'x-binning': 1,
    'y-binning': Binning.NoBinning,
    'exposure': 1e-3,
    'gain': Gain.Low,
    'bytes-per-pixel': BytesPerPixel.Four,
    'readout-speed': ReadoutSpeed.KHz_250,
}


class GreatEyesActivities(IntFlag):
    CoolingDown = auto()
    WarmingUp = auto()
    Exposing = auto()
    StartingUp = auto()
    ShuttingDown = auto()
    SettingParameters = auto()


class GreatEyes(SwitchedPowerDevice, NetworkedDevice, Component):

    def __init__(self, _id: int):
        self._initialized = False
        self.detected = False
        self.connected = False
        Component.__init__(self)

        self.id = int(_id)
        self.addr = self.id - 1
        self.errors = []

        self.conf: dict = deepcopy(Config().toml['deepspec']['cameras'])           # all-cameras configuration
        specific_conf: dict = Config().toml['deepspec']['camera'][str(self.id)]    # this camera configuration

        self.conf.update(specific_conf)
        NetworkedDevice.__init__(self, self.conf)

        self.band = self.conf['band']
        self.logger = logging.getLogger(f"mast.spec.deepspec.camera.{self.band}")
        init_log(self.logger, logging.DEBUG)

        self.enabled = bool(self.conf['enabled']) if 'enabled' in self.conf else False

        self.target_cool_temp = self.conf['target_cool_temp'] if 'target_cool_temp' in self.conf else -80
        self.target_warm_temp = self.conf['target_warm_temp'] if 'target_warm_temp' in self.conf else 0

        if 'readout_speed' in self.conf:
            speed_name = self.conf['readout_speed']
            if speed_name in ReadoutSpeed.__members__:
                self.readout_speed = ReadoutSpeed[speed_name].value
            else:
                self.logger.error(f"bad 'readout_speed' '{speed_name}' in config file. " +
                                  f"Valid names: {list(ReadoutSpeed.__members__)})")
        else:
            self.readout_speed = ReadoutSpeed.KHz_250

        self.acquisition: str | None = None
        boot_delay = self.conf['boot_delay'] if 'boot_delay' in self.conf else 20

        self.power = SwitchedPowerDevice(self.conf)
        self.detected = False
        if not self.enabled:
            self.logger.error(f"camera {self.name} is disabled")
            return

        if not self.power.switch.detected:
            return

        if self.power.is_off():
            self.power_on()
        else:
            self.power.switch.cycle(self.power.outlet)

        self.logger.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
        time.sleep(boot_delay)

        ret = ge.DisconnectCamera(addr=self.addr)
        self.logger.debug(f"ge.DisconnectCamera(addr={self.addr}) -> {ret}")
        ret = ge.DisconnectCameraServer(addr=self.addr)
        self.logger.debug(f"ge.DisconnectCameraServer(addr={self.addr}) -> {ret}")

        ret = ge.SetupCameraInterface(ge.connectionType_Ethernet, ipAddress=self.ipaddress, addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                              f"ipaddress={self.ipaddress}, addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")
            return
        self.logger.debug(f"OK: ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                          f"ipaddress={self.ipaddress}, addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")

        ret = ge.ConnectToSingleCameraServer(addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.ConnectToSingleCameraServer(addr={self.addr}) " +
                              f"(ret={ret}, msg='{ge.StatusMSG}')")
            return
        self.logger.debug(f"OK: ge.ConnectToSingleCameraServer(addr={self.addr}) " +
                          f"(ret={ret}, msg='{ge.StatusMSG}')")

        self.detected = True
        model = []
        ret = ge.ConnectCamera(model=model, addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.ConnectCamera(model=[], addr={self.addr}) (ret={ret}, " +
                              f"msg='{ge.StatusMSG}')")
            return
        self.logger.debug(f"OK: ge.ConnectCamera(model=[], addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")
        self.connected = True

        self.model_id = model[0]
        self.model = model[1]

        self.firmware_version = ge.GetFirmwareVersion(addr=self.addr)

        ret = ge.InitCamera(addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.InitCamera(addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")
            ge.DisconnectCamera(addr=self.addr)
            ge.DisconnectCameraServer(addr=self.addr)
            return

        info = ge.TemperatureControl_Init(addr=self.addr)
        self.min_temp = info[0]
        self.max_temp = info[1]

        info = ge.GetImageSize(addr=self.addr)
        self.x_size = info[0]
        self.y_size = info[1]
        self.bytes_per_pixel = info[2]

        self.pixel_size_microns = ge.GetSizeOfPixel(addr=self.addr)

        ret = ge.SetBitDepth(4, addr=self.addr)
        if not ret:
            self.logger.error(f"failed to ge.SetBitDepth(4, addr={self.addr}) ({ret=})")
        self.logger.info(f"OK: ge.SetBitDepth(4, addr={self.addr})")

        self.set_led(False)

        self.last_backside_temp_check: datetime.datetime | None = None
        self.backside_temp_check_interval = self.conf['backside_temp_check_interval']\
            if 'backside_temp_interval' in self.conf else 30
        self.backside_temp_safe = True

        self.latest_exposure_time: float = 0
        self.readout_thread: threading.Thread | None = None
        
        self.latest_exposure_utc_start: datetime | None = None
        self.latest_exposure_utc_mid: datetime | None = None
        self.latest_exposure_utc_end: datetime | None = None
        
        self.latest_exposure_local_start: datetime | None = None
        self.latest_exposure_local_mid: datetime | None = None
        self.latest_exposure_local_end: datetime | None = None

        self.x_binning = 1
        self.y_binning = 1

        self.readout_amplifiers: ReadoutAmplifiers = defaults['readout-amplifiers']
        self.gain: Gain = Gain.Low

        self.timer = RepeatTimer(1, function=self.on_timer)
        self.timer.name = f'deepspec-camera-{self.band}-timer-thread'
        self.timer.start()

        self._initialized = True

    @property
    def name(self) -> str:
        return f'deepspec-{self.band}'

    def set_led(self, on_off: bool):
        if not self.detected:
            return

        ret = ge.SetLEDStatus(on_off, addr=self.addr)
        if not ret:
            self.logger.error(f"could not set back LED to {'ON' if on_off else 'OFF'}")

    def __repr__(self):
        return (f"<Greateyes>(band={self.band}, id={self.id}, address='{self.ipaddress}', model='{self.model}', " +
                f"model_id='{self.model_id}', firmware_version={self.firmware_version})")

    def __del__(self):
        if self.addr is None:
            return
        if not self.detected:
            return
        ge.DisconnectCamera(addr=self.addr)
        ge.DisconnectCameraServer(addr=self.addr)

    def append_error(self, err):
        self.errors.append(err)
        self.logger.error(err)

    def power_off(self):
        if self.power.switch:
            self.power.switch.off(self.power.outlet)

    def status(self) -> dict:
        ret = {
            'id': self.id,
            'ipaddr': self.ipaddress,
            'detected': self.detected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'enabled': self.enabled,
            'band': self.band,
        }
        if self.enabled and self.detected:
            ret['powered'] = self.power.is_on()
            ret['connected'] = self.connected
            ret['addr'] = self.addr
            ret['activities'] = self.activities
            ret['activities_verbal'] = self.activities.__repr__()
            ret['idle'] = self.is_idle()
            ret['min_temp'] = self.min_temp
            ret['max_temp'] = self.max_temp
            ret['front_temperature'] = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
            ret['back_temperature'] = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.addr)
            ret['errors'] = self.errors
        return ret

    def cool_down(self):
        if not self.detected:
            return

        if ge.TemperatureControl_SetTemperature(temperature=self.target_cool_temp, addr=self.addr):
            self.start_activity(GreatEyesActivities.CoolingDown)

    def warm_up(self):
        if not self.detected:
            return
        if ge.TemperatureControl_SetTemperature(temperature=self.max_temp, addr=self.addr):
            self.start_activity(GreatEyesActivities.WarmingUp)

    def startup(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.StartingUp)
        self.cool_down()

    def shutdown(self):
        if not self.detected:
            return
        self.start_activity(GreatEyesActivities.ShuttingDown)
        self.warm_up()

    def set_parameters(self,
                       readout_amplifiers_: readout_amplifiers | None,
                       y_binning_: binnings | None,
                       gain_: gains | None,
                       bytes_per_pixel_: bytes_per_pixels | None,
                       readout_speed_: readout_speeds | None,
                       save: bool = False
                       ):

        self.errors = []
        if not self.detected:
            self.errors.append(f"not detected")
            return

        self.start_activity(GreatEyesActivities.SettingParameters)

        if readout_amplifiers_:
            _readout_amplifiers = readout_amplifiers_
        else:
            _readout_amplifiers = self.conf['readout_amplifiers'] if 'readout_amplifiers' in self.conf \
                else defaults['readout-amplifiers']
        self.readout_amplifiers = ReadoutAmplifiers(_readout_amplifiers)
        ret = ge.SetupSensorOutputMode(self.readout_amplifiers, addr=self.addr)
        if ret:
            self.logger.info(f"set sensor output mode to {self.readout_amplifiers=}")
        else:
            self.append_error(f"failed to set sensor output mode to {self.readout_amplifiers=} ({ret=})")

        # self.x_binning = self.conf['x_binning'] if 'x_binning' in self.conf else defaults['x-binning']
        self.x_binning = Binning.NoBinning
        if y_binning_:
            _y_binning = y_binning_
        else:
            _y_binning = self.conf['y_binning'] if 'y_binning' in self.conf else defaults['y-binning']
        self.y_size = Binning(_y_binning)
        ret = ge.SetBinningMode(self.x_binning, self.y_binning, addr=self.addr)
        if ret:
            self.logger.info(f"set binning to {self.x_binning=}, {self.y_binning=}")
        else:
            self.append_error(f"failed to set binning to {self.x_binning=}, {self.y_binning=} ({ret=})")

        if gain_:
            _gain = gain_
        else:
            _gain = self.conf['gain'] if 'gain' in self.conf else defaults['gain']
        self.gain = Gain(_gain)
        ret = ge.SetupGain(self.gain, addr=self.addr)
        if ret:
            self.logger.info(f"set gain to {self.gain}")
        else:
            self.append_error(f"failed to set gain to {self.gain} ({ret=})")

        if bytes_per_pixel_:
            self.bytes_per_pixel = bytes_per_pixel_
        else:
            self.bytes_per_pixel = self.conf['bytes_per_pixel'] if 'bytes_per_pixel' in self.conf \
                else defaults['bytes-per-pixel']
        ret = ge.SetBitDepth(self.bytes_per_pixel, addr=self.addr)
        if ret:
            self.logger.info(f"set bit_depth to {self.bytes_per_pixel}")
        else:
            self.append_error(f"failed to set bit_depth to {self.bytes_per_pixel} ({ret=})")

        if readout_speed_:
            _readout_speed = readout_speed_
        else:
            _readout_speed = self.conf['readout_speed'] if 'readout_speed' in self.conf \
                else defaults['readout-speed']
        self.readout_speed = ReadoutSpeed(_readout_speed)
        ret = ge.SetReadOutSpeed(self.readout_speed, addr=self.addr)
        if ret:
            self.logger.info(f"set readout speed to {self.readout_speed}")
        else:
            self.append_error(f"could not set readout speed to {self.readout_speed} ({ret=})")

        if save:
            pass

    def expose(self, seconds: float | None = None, acquisition: str | None = None):

        self.errors = []
        if not self.detected:
            self.errors.append("not detected")
            return

        if not self.is_idle():
            if ge.DllIsBusy(addr=self.addr):
                self.append_error("could not start exposure: ge.DllIsBusy()")
                return

            if self.is_active(GreatEyesActivities.CoolingDown):
                ret = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
                if ret == FAILED_TEMPERATURE:
                    self.append_error(f"could not read sensor temperature ({ret=})")
                else:
                    delta_temp = abs(self.conf['target_cool_temp'] - ret)
                    self.append_error(f"cannot expose while cooling down ({delta_temp=})")
                return

            if not self.is_idle():
                self.append_error(f"camera is active ({self.activities=})")
                return

        if seconds is None:
            self.latest_exposure_time = self.conf['exposure'] if 'exposure' in self.conf else None
        if seconds is None:
            raise Exception(f"cannot figure out exposure time")
        self.latest_exposure_time = seconds

        self.acquisition = acquisition

        ret = ge.SetExposure(self.latest_exposure_time, addr=self.addr)
        if not ret:
            self.append_error(f"could not ge.SetExposure({seconds=}, addr={self.addr}) ({ret=})")
            return

        ret = ge.StartMeasurement_DynBitDepth(addr=self.addr)
        if ret:
            self.start_activity(GreatEyesActivities.Exposing)
            self.latest_exposure_utc_start = datetime.datetime.now(datetime.UTC)
            self.latest_exposure_local_start = datetime.datetime.now()
        else:
            self.append_error(f"could not ge.StartMeasurement_DynBitDepth(addr={self.addr}) ({ret=})")

    def readout(self):
        if not self.detected:
            return

        imageArray = ge.GetMeasurementData_DynBitDepth(addr=self.addr)
        hdr = {}
        for key in FITS_STANDARD_FIELDS.keys():
            hdr[key] = FITS_STANDARD_FIELDS[key]
        hdr['BAND'] = 'DeepSpec_' + self.band
        hdr['CAMERA_IP'] = self.conf['ipaddr']
        hdr['TYPE'] = 'RAW'
        
        hdr['LOCAL_T_START'] = f"{self.latest_exposure_local_start:FITS_DATE_FORMAT}"
        hdr['LOCAL_T_MID'] = f"{self.latest_exposure_local_mid:FITS_DATE_FORMAT}"
        hdr['LOCAL_T_END'] = f"{self.latest_exposure_local_mid:FITS_DATE_FORMAT}"
        
        hdr['T_START'] = f"{self.latest_exposure_utc_start:FITS_DATE_FORMAT}"
        hdr['T_MID'] = f"{self.latest_exposure_utc_mid:FITS_DATE_FORMAT}"
        hdr['T_END'] = f"{self.latest_exposure_utc_end:FITS_DATE_FORMAT}"
        
        hdr['T_EXP'] = self.latest_exposure_time
        hdr['TEMP_GOAL'] = self.target_cool_temp
        hdr['TEMP_SAFE_FLAG'] = self.backside_temp_safe
        hdr['DATE-OBS'] = hdr['T_MID']
        hdr['MJD-OBS'] = atime.Time(self.latest_exposure_utc_mid).mjd
        hdr['READOUT_SPEED'] = readout_speed_names[self.readout_speed]
        hdr['CDELT1'] = self.x_binning
        hdr['CDELT2'] = self.y_binning
        hdr['NAXIS'] = 2
        hdr['NAXIS1'] = self.x_size
        hdr['NAXIS2'] = self.y_size
        hdr['PIXEL_SIZE'] = self.pixel_size_microns
        hdr['BITPIX'] = self.bytes_per_pixel
        for key in hdr.keys():
            hdr[key] = (hdr[key], FITS_HEADER_COMMENTS[key])
        HDR = fits.Header()
        for key in hdr.keys():
            HDR[key] = hdr[key]
        hdu = fits.PrimaryHDU(imageArray, header=HDR)
        hdul = fits.HDUList([hdu])

        filename = PathMaker().make_exposure_file_name(camera=f"deepspec.{self.band}", acquisition=self.acquisition)
        try:
            hdul.writeto(filename)
            self.logger.info(f"saved exposure to '{filename}'")
        except Exception as e:
            self.logger.exception(f"failed to save exposure", exc_info=e)

    def abort(self):
        if not self.detected:
            return

        if not ge.DllIsBusy(addr=self.addr):
            return
        ret = ge.StopMeasurement(addr=self.addr)
        if not ret:
            self.append_error(f"could not ge.StopMeasurement(addr={self.addr})")

    def on_timer(self):
        """
        Called periodically by a timer.
        Checks if any in-progress activities can be ended.
        """

        if not self.detected:
            return

        now = datetime.now()
        if (self.last_backside_temp_check is None or (now - self.last_backside_temp_check) >
                timedelta(seconds=self.backside_temp_check_interval)):

            ret = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.addr)
            if ret == FAILED_TEMPERATURE:
                self.logger.error(f"failed to read back temperature ({ret=})")
            else:
                if ret >= 55:
                    self.backside_temp_safe = False
                    self.logger.error(f"back side temperature too high: {ret} degrees celsius")
                else:
                    self.backside_temp_safe = True

            self.last_backside_temp_check = now

        if self.is_active(GreatEyesActivities.Exposing):
            # check if exposure has ended
            if not ge.DllIsBusy(addr=self.addr):
                self.end_activity(GreatEyesActivities.Exposing)
                
                self.latest_exposure_local_end = datetime.now()
                self.latest_exposure_local_mid = (self.latest_exposure_local_start +
                                                  (self.latest_exposure_local_end - self.latest_exposure_local_start) / 2)
                
                self.latest_exposure_utc_end = datetime.now(datetime.UTC)                
                self.latest_exposure_utc_mid = (self.latest_exposure_utc_start + 
                                                (self.latest_exposure_utc_end - self.latest_exposure_utc_start) / 2)
                self.readout_thread = threading.Thread(name=f'deepspec-camera-{self.band}-readout-thread',
                                                       target=self.readout)
                self.readout_thread.start()
            else:
                elapsed = now - self.timings[GreatEyesActivities.Exposing].start_time
                max_expected = self.latest_exposure_time + 10
                if elapsed > max_expected:
                    self.append_error(f"exposure takes too long ({elapsed=} > {max_expected=})")
                    ret = ge.StopMeasurement(addr=self.addr)
                    if ret:
                        self.append_error(f"could not ge.StopMeasurement(addr={self.addr}) ({ret=})")
                    else:
                        self.end_activity(GreatEyesActivities.Exposing)

        if self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp):
            front_temp = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
            if front_temp == FAILED_TEMPERATURE:
                self.append_error(f"failed reading sensor temperature")
            else:
                switch_temp_control_off = False
                power_off = False
                if self.is_active(GreatEyesActivities.CoolingDown) and abs(front_temp - self.target_cool_temp) <= 1:
                    self.end_activity(GreatEyesActivities.CoolingDown)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(GreatEyesActivities.StartingUp)
                    switch_temp_control_off = True

                if self.is_active(GreatEyesActivities.WarmingUp) and abs(front_temp >= self.target_warm_temp) <= 1:
                    self.end_activity(GreatEyesActivities.WarmingUp)
                    if self.is_active(GreatEyesActivities.ShuttingDown):
                        self.end_activity(GreatEyesActivities.ShuttingDown)
                        power_off = True
                    switch_temp_control_off = True

                if switch_temp_control_off:
                    ret = ge.TemperatureControl_SwitchOff(addr=self.addr)
                    if ret:
                        self.logger.info(f"OK: ge.TemperatureControl_SwitchOff(addr={self.addr})")
                    else:
                        self.logger.error(f"could not ge.TemperatureControl_SwitchOff(addr={self.addr}) (ret={ret})")

                if power_off:
                    self.power_off()
                    self.timer.finished.set()

    @property
    def operational(self) -> bool:
        return self.power.switch.detected and self.detected and not \
            (self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = f"{self.name}"
        if not self.power.switch.detected:
            ret.append(f"{label}: power switch (at {self.power.switch.ipaddress}) not detected")
        if not self.detected:
            ret.append(f"{label} camera (at {self.ipaddress}) not detected")
        if self.is_active(GreatEyesActivities.CoolingDown):
            ret.append(f'{label} camera is CoolingDown')
        if self.is_active(GreatEyesActivities.WarmingUp):
            ret.append(f'{label} camera is WarmingUp')

        return ret


class DeepSpec(Component):

    cameras = []
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(DeepSpec, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        Component.__init__(self)
        self.conf = Config().toml['deepspec']['camera']
        self.configured_cameras = list(self.conf.keys())
        self.cameras = make_deepspec_cameras()

    def status(self):
        ret = {}
        for num in self.configured_cameras:
            band = self.conf[num]['band']
            found = [cam for cam in self.cameras if cam.band == band]
            if len(found) == 0:
                ret[band] = {
                    'ipaddr': self.conf[num]['network']['address'],
                    'detected': False,
                }
            else:
                cam = found[0]
                ret[band] = {
                    'detected': True,
                    'enabled': cam.enabled,
                }
                for k, v in cam.status().items():
                    ret[band][k] = v
        return ret

    def name(self) -> str:
        return 'deepspec'

    def startup(self):
        pass

    def shutdown(self):
        pass

    def abort(self):
        pass

    @property
    def operational(self) -> bool:
        not_detected = [cam for cam in self.cameras if not cam.detected]
        return len(not_detected) == 0

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        for cam in self.cameras:
            for reason in cam.why_not_operational:
                ret.append(reason)
        return ret


def make_deepspec_cameras() -> List[GreatEyes]:

    configured_camera_ids = list(Config().toml['deepspec']['camera'].keys())
    cams: List[GreatEyes | None] = []

    for camera_id in configured_camera_ids:
        cam = GreatEyes(_id=camera_id)
        cams.append(cam)
        if cam.connected:
            cam.startup()
    return cams


#
# FastAPI
#

deepspec = DeepSpec()


def list_cameras():
    return deepspec.cameras


def status() -> dict:
    return deepspec.status()


def bands() -> List[str]:
    return list(Band.__members__.keys())


def startup():
    for camera in deepspec.cameras:
        threading.Thread(
                name=f"camera-{camera.band}-startup",
                target=camera.startup,
            ).start()


def shutdown():
    for camera in deepspec.cameras:
        threading.Thread(
                name=f"camera-{camera.band}-shutdown",
                target=camera.shutdown,
            ).start()


def abort():
    for camera in deepspec.cameras:
        camera.abort()


def set_params(
        readout_amplifier: readout_amplifiers = Query(description='', default=defaults['readout-amplifiers']),
        y_binning: binnings = Query(description='Vertical binning', default=defaults['y-binning']),
        gain: gains = Query(description='Gain', default=defaults['gain']),
        bytes_per_pixel: bytes_per_pixels = Query(description='Bytes per pixel', default=defaults['bytes-per-pixel']),
        readout_speed: readout_speeds = Query(description='Readout speed', default=defaults['readout-speed']),
        save: bool = Query(description='Save these values to the configuration file?', default=False)
):
    for camera in deepspec.cameras:
        threading.Thread(
            name=f"camera-{camera.band}-set-parameters",
            target=camera.set_parameters,
            args=[
                getattr(ReadoutAmplifiers, readout_amplifier.name),
                getattr(Binning, y_binning.name),
                getattr(Gain, gain.name),
                getattr(BytesPerPixel, bytes_per_pixel.name),
                getattr(ReadoutSpeed, readout_speed.name),
                save,
            ]
        ).start()


def set_camera_params(band: Band,
                      readout_amplifier: readout_amplifiers = Query(description='',
                                                                    default=defaults['readout-amplifiers'].name),
                      y_binning: binnings = Query(description='Vertical binning', default=defaults['y-binning'].name),
                      gain: gains = Query(description='Gain', default=defaults['gain'].name),
                      bytes_per_pixel: bytes_per_pixels = Query(description='Bytes per pixel',
                                                                default=defaults['bytes-per-pixel'].name),
                      readout_speed: readout_speeds = Query(description='Readout speed',
                                                            default=defaults['readout-speed'].name),
                      save: bool = Query(description='Save these values to the configuration file?', default=False)
                      ):
    found = [cam for cam in deepspec.cameras if cam.band == band]
    if len(found) == 0:
        return {'Error': f"No camera for band '{band}'"}

    camera = found[0]
    threading.Thread(
        name=f"camera-{camera.band}-set-parameters",
        target=camera.set_parameters,
        args=[
            getattr(ReadoutAmplifiers, readout_amplifier.name),
            getattr(Binning, y_binning.name),
            getattr(Gain, gain.name),
            getattr(BytesPerPixel, bytes_per_pixel.name),
            getattr(ReadoutSpeed, readout_speed.name),
            save,
        ]
    ).start()


def expose(seconds: float):
    for camera in deepspec.cameras:
        threading.Thread(
            name=f"camera-{camera.band}-exposure-{seconds}sec",
            target=camera.expose,
            args=[seconds]
        ).start()


def camera_expose(band: Band, seconds: float):
    found = [cam for cam in deepspec.cameras if cam.band == band]
    if len(found) == 0:
        return {'Error': f"No camera for band '{band}'"}

    camera = found[0]
    threading.Thread(
        name=f"camera-{camera.band}-exposure-{seconds}sec",
        target=camera.expose,
        args=[seconds]
    ).start()


base_path = BASE_SPEC_PATH + 'deepspec/cameras/'
tag = 'DeepSpec Cameras'
router = fastapi.APIRouter()

router.add_api_route(base_path + 'list', tags=[tag], endpoint=list_cameras)
router.add_api_route(base_path + 'bands', tags=[tag], endpoint=bands)
router.add_api_route(base_path + 'status', tags=[tag], endpoint=status)
router.add_api_route(base_path + 'set_params', tags=[tag], endpoint=set_params)
router.add_api_route(base_path + 'set_camera_params', tags=[tag], endpoint=set_camera_params)
router.add_api_route(base_path + 'expose', tags=[tag], endpoint=expose)
router.add_api_route(base_path + 'camera_expose', tags=[tag], endpoint=camera_expose)
router.add_api_route(base_path + 'startup', tags=[tag], endpoint=startup)
router.add_api_route(base_path + 'shutdown', tags=[tag], endpoint=shutdown)
router.add_api_route(base_path + 'abort', tags=[tag], endpoint=abort)

if __name__ == "__main__":
    cameras: List[GreatEyes] = make_deepspec_cameras()

    for c in deepspec.cameras:
        print(c)
        c.power_off()
