import datetime
import threading
import time

import fastapi
from fastapi import Query

from common.config import Config
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.spec import SpecCameraExposureSettings, DeepspecBands
from common.filer import Filer
import sys
import os
import logging
from common.utils import Component, function_name
from common.mast_logging import init_log
from common.networking import NetworkedDevice
from typing import List, get_args
from common.utils import RepeatTimer, BASE_SPEC_PATH
from enum import IntFlag, auto, Enum

import astropy.io.fits as fits
import astropy.time as atime
from fits import FITS_HEADER_COMMENTS, FITS_STANDARD_FIELDS

sys.path.append(os.path.join(os.path.dirname(__file__), 'sdk'))
import cameras.greateyes.sdk.greateyesSDK as ge

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
    ReadingOut = auto()
    Saving = auto()
    StartingUp = auto()
    ShuttingDown = auto()
    SettingParameters = auto()
    Probing = auto()


class GreatEyes(SwitchedOutlet, NetworkedDevice, Component):

    def __init__(self, band: DeepspecBands):
        self._initialized = False
        self._detected = False
        self._connected = False
        Component.__init__(self)

        self.band = band
        self.conf = Config().get_specs()['deepspec'][self.band]    # specific to this camera instance
        self.ge_device = self.conf['device']
        self._name = f"Deepspec-{self.band}"
        self.outlet_name = f"Deepspec{self.band}"
        self.errors = []

        NetworkedDevice.__init__(self, self.conf)
        SwitchedOutlet.__init__(self, outlet_name=f'{self.outlet_name}', domain=OutletDomain.Spec)

        self.enabled = bool(self.conf['enabled']) if 'enabled' in self.conf else False

        self.target_cool_temp = self.conf['target_cool_temp'] if 'target_cool_temp' in self.conf else -80
        self.target_warm_temp = self.conf['target_warm_temp'] if 'target_warm_temp' in self.conf else 0

        if 'readout_speed' in self.conf:
            speed_name = self.conf['readout_speed']
            if speed_name in ReadoutSpeed.__members__:
                self.readout_speed = ReadoutSpeed[speed_name].value
            else:
                self.error(f"bad 'readout_speed' '{speed_name}' in config file. " +
                                  f"Valid names: {list(ReadoutSpeed.__members__)})")
        else:
            self.readout_speed = ReadoutSpeed.KHz_250

        self.acquisition: str | None = None

        self.last_backside_temp_check: datetime.datetime | None = None
        self.backside_temp_check_interval = self.conf['backside_temp_check_interval']\
            if 'backside_temp_interval' in self.conf else 30
        self.backside_temp_safe = True

        self.readout_thread: threading.Thread | None = None

        self.latest_exposure_utc_start: datetime.datetime | None = None
        self.latest_exposure_utc_mid: datetime.datetime | None = None
        self.latest_exposure_utc_end: datetime.datetime | None = None

        self.latest_exposure_local_start: datetime.datetime | None = None
        self.latest_exposure_local_mid: datetime.datetime | None = None
        self.latest_exposure_local_end: datetime.datetime | None = None

        self.x_binning = 1
        self.y_binning = 1

        self.readout_amplifiers: ReadoutAmplifiers = defaults['readout-amplifiers']
        self.gain: Gain = Gain.Low

        self.model_id = None
        self.model = None
        self.firmware_version = None

        self.latest_settings: SpecCameraExposureSettings | None = None

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
        self.probe_interval_seconds = self.conf['probe_interval_seconds'] \
            if 'probe_interval_seconds' in self.conf else 60
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
            self.error(f"Could not ge.InitCamera(addr={self.ge_device}) (ret={ret}, msg='{ge.StatusMSG}')")
            ge.DisconnectCamera(addr=self.ge_device)
            ge.DisconnectCameraServer(addr=self.ge_device)
            self.end_activity(GreatEyesActivities.Probing)
            return

        info = ge.TemperatureControl_Init(addr=self.ge_device)
        self.min_temp = info[0]
        self.max_temp = info[1]

        info = ge.GetImageSize(addr=self.ge_device)
        self.x_size = info[0]
        self.y_size = info[1]
        self.bytes_per_pixel = info[2]

        self.pixel_size_microns = ge.GetSizeOfPixel(addr=self.ge_device)

        ret = ge.SetBitDepth(4, addr=self.ge_device)
        if not ret:
            self.error(f"failed to ge.SetBitDepth(4, addr={self.ge_device}) ({ret=})")
        self.info(f"OK: ge.SetBitDepth(4, addr={self.ge_device})")

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
            }
        return ret

    def cool_down(self):
        if not self.detected:
            return

        if ge.TemperatureControl_SetTemperature(temperature=self.target_cool_temp, addr=self.ge_device):
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
        ret = ge.SetupSensorOutputMode(self.readout_amplifiers, addr=self.ge_device)
        if ret:
            self.info(f"set sensor output mode to {self.readout_amplifiers=}")
        else:
            self.append_error(f"failed to set sensor output mode to {self.readout_amplifiers=} ({ret=})")

        # self.x_binning = self.conf['x_binning'] if 'x_binning' in self.conf else defaults['x-binning']
        self.x_binning = Binning.NoBinning
        if y_binning_:
            _y_binning = y_binning_
        else:
            _y_binning = self.conf['y_binning'] if 'y_binning' in self.conf else defaults['y-binning']
        self.y_size = Binning(_y_binning)
        ret = ge.SetBinningMode(self.x_binning, self.y_binning, addr=self.ge_device)
        if ret:
            self.info(f"set binning to {self.x_binning=}, {self.y_binning=}")
        else:
            self.append_error(f"failed to set binning to {self.x_binning=}, {self.y_binning=} ({ret=})")

        if gain_:
            _gain = gain_
        else:
            _gain = self.conf['gain'] if 'gain' in self.conf else defaults['gain']
        self.gain = Gain(_gain)
        ret = ge.SetupGain(self.gain, addr=self.ge_device)
        if ret:
            self.info(f"set gain to {self.gain}")
        else:
            self.append_error(f"failed to set gain to {self.gain} ({ret=})")

        if bytes_per_pixel_:
            self.bytes_per_pixel = bytes_per_pixel_
        else:
            self.bytes_per_pixel = self.conf['bytes_per_pixel'] if 'bytes_per_pixel' in self.conf \
                else defaults['bytes-per-pixel']
        ret = ge.SetBitDepth(self.bytes_per_pixel, addr=self.ge_device)
        if ret:
            self.info(f"set bit_depth to {self.bytes_per_pixel}")
        else:
            self.append_error(f"failed to set bit_depth to {self.bytes_per_pixel} ({ret=})")

        if readout_speed_:
            _readout_speed = readout_speed_
        else:
            _readout_speed = self.conf['readout_speed'] if 'readout_speed' in self.conf \
                else defaults['readout-speed']
        self.readout_speed = ReadoutSpeed(_readout_speed)
        ret = ge.SetReadOutSpeed(self.readout_speed, addr=self.ge_device)
        if ret:
            self.info(f"set readout speed to {self.readout_speed}")
        else:
            self.append_error(f"could not set readout speed to {self.readout_speed} ({ret=})")

        if save:
            pass

    def expose(self, settings: SpecCameraExposureSettings):
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
                    delta_temp = abs(self.conf['target_cool_temp'] - ret)
                    self.append_error(f"cannot expose while cooling down ({delta_temp=})")
                return

            if not self.is_idle():
                self.append_error(f"camera is active ({self.activities=})")
                return

        if settings.duration is None:
            settings.duration = self.conf['exposure'] if 'exposure' in self.conf else None
        if settings.duration is None:
            raise Exception(f"cannot figure out exposure time")
        self.latest_settings = settings

        ret = ge.SetExposure(self.latest_settings.exposure_duration, addr=self.ge_device)
        if not ret:
            self.append_error(f"could not ge.SetExposure({settings.duration=}, addr={self.ge_device}) ({ret=})")
            return

        ret = ge.StartMeasurement_DynBitDepth(addr=self.ge_device)
        if ret:
            self.start_activity(GreatEyesActivities.Exposing)
            self.latest_exposure_utc_start = datetime.datetime.now(datetime.UTC)
            self.latest_exposure_local_start = datetime.datetime.now()
        else:
            self.append_error(f"could not ge.StartMeasurement_DynBitDepth(addr={self.ge_device}) ({ret=})")

    def readout(self):
        if not self.detected:
            return

        if not self.latest_settings.output_folder:
            pass # make a folder?

        self.start_activity(GreatEyesActivities.ReadingOut)
        image_array = ge.GetMeasurementData_DynBitDepth(addr=self.ge_device)
        self.end_activity(GreatEyesActivities.ReadingOut)

        self.start_activity(GreatEyesActivities.Saving)
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
        
        hdr['T_EXP'] = self.latest_settings.exposure_duration
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
        for key in list(hdr.keys()):
            hdr[key] = (hdr[key], FITS_HEADER_COMMENTS[key])
        header = fits.Header()
        for key in hdr.keys():
            header[key] = hdr[key]
        hdu = fits.PrimaryHDU(image_array, header=header)
        hdul = fits.HDUList([hdu])

        filename = os.path.join(Filer().ram.root, self.latest_settings.output_folder, self.name)
        if self.latest_settings.number_in_sequence:
            filename += f"_{self.latest_settings.number_in_sequence}"
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
                 datetime.datetime.now() - self.last_probe_time > datetime.timedelta(seconds=self.probe_interval_seconds))):
            self.last_probe_time = datetime.datetime.now()
            self.probe()
            return

        if not self.detected:
            return

        now = datetime.datetime.now()
        if (self.last_backside_temp_check is None or (now - self.last_backside_temp_check) >
                datetime.timedelta(seconds=self.backside_temp_check_interval)):

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
                
                self.latest_exposure_local_end = datetime.datetime.now()
                self.latest_exposure_local_mid = (self.latest_exposure_local_start +
                                              (self.latest_exposure_local_end - self.latest_exposure_local_start) / 2)
                
                self.latest_exposure_utc_end = datetime.datetime.now(datetime.UTC)
                self.latest_exposure_utc_mid = (self.latest_exposure_utc_start + 
                                                (self.latest_exposure_utc_end - self.latest_exposure_utc_start) / 2)
                self.readout_thread = threading.Thread(name=f'deepspec-camera-{self.band}-readout-thread',
                                                       target=self.readout)
                self.readout_thread.start()
            else:
                elapsed = now - self.timings[GreatEyesActivities.Exposing].start_time
                max_expected = self.latest_settings.exposure_duration + 10
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
                    ret = ge.TemperatureControl_SwitchOff(addr=self.ge_device)
                    if ret:
                        self.info(f"OK: ge.TemperatureControl_SwitchOff(addr={self.ge_device})")
                    else:
                        self.error(f"could not ge.TemperatureControl_SwitchOff(addr={self.ge_device}) (ret={ret})")

                if power_off:
                    self.power_off()
                    self.timer.finished.set()

    @property
    def operational(self) -> bool:
        return self.power_switch.detected and self.detected and not \
            (self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp))

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
    found = [cam for cam in cameras if cam.band == band]
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


def camera_expose(band: DeepspecBands, seconds: float):

    camera = [cam for cam in cameras if cam.band == band][0]
    if not camera.detected:
        return

    threading.Thread(
        name=f"camera-{camera.band}-exposure-{seconds}sec",
        target=camera.expose,
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

router.add_api_route(base_path + 'set_camera_params', tags=[tag], endpoint=set_camera_params)
router.add_api_route(base_path + 'camera_expose', tags=[tag], endpoint=camera_expose)

if __name__ == "__main__":
    for c in cameras:
        print(c)
        c.power_off()
