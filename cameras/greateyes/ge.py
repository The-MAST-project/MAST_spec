import datetime
import threading
import time

from dlipower.dlipower.dlipower import SwitchedPowerDevice
from config.config import Config
import sys
import os
import logging
from utils import init_log
from networking import NetworkedDevice
from typing import List
from copy import deepcopy
from utils import Activities, RepeatTimer
from enum import IntFlag, auto, Enum
from datetime import timedelta, datetime

import astropy.io.fits as fits
import astropy.time as atime
from fits import FITS_DATE_FORMAT, FITS_HEADER_COMMENTS, FITS_STANDARD_FIELDS

sys.path.append(os.path.join(os.path.dirname(__file__), 'sdk'))
import sdk.greateyesSDK as ge

logger = logging.getLogger('greateyes')
init_log(logger, logging.DEBUG)

dll_version = ge.GetDLLVersion()

FAILED_TEMPERATURE = -300


class ReadoutAmplifiers(Enum):
    OSR = 0,
    OSL = 1,
    BOTH = 2,


class Gain(Enum):
    Low = 0,    # Low ( Max. Dyn. Range )
    High = 1,   # Std ( High Sensitivity )


class ReadoutSpeed(Enum):
    KHz_50 = ge.readoutSpeed_50_kHz
    KHz_100 = ge.readoutSpeed_100_kHz
    KHz_250 = ge.readoutSpeed_250_kHz
    KHz_500 = ge.readoutSpeed_500_kHz
    MHz_1 = ge.readoutSpeed_1_MHz
    MHz_3 = ge.readoutSpeed_3_MHz


readout_speed_names = {
    ReadoutSpeed.MHz_1:       '1 MHz',
    ReadoutSpeed.MHz_3:       '3 MHz',
    ReadoutSpeed.KHz_500:   '500 KHz',
    ReadoutSpeed.KHz_250:   '250 KHz',
    ReadoutSpeed.KHz_100:   '100 KHz',
    ReadoutSpeed.KHz_50:     '50 KHz',
}

defaults = {
    'readout_amplifiers': ReadoutAmplifiers.OSR,
    'x_binning': 1,
    'y_binning': 1,
    'exposure': 1e-3,
    'gain': Gain.Low,
    'bit_depth': 4,
}


class GreatEyesActivities(IntFlag):
    CoolingDown = auto()
    WarmingUp = auto()
    Exposing = auto()
    StartingUp = auto()
    ShuttingDown = auto()


class GreatEyes(SwitchedPowerDevice, NetworkedDevice, Activities):

    def __init__(self, _id: int):
        self._initialized = False
        self.connected = False

        self.id = int(_id)
        self.addr = self.id - 1

        self.conf: dict = deepcopy(Config().toml['deepspec']['cameras'])           # all-cameras configuration
        specific_conf: dict = Config().toml['deepspec']['camera'][str(self.id)]    # this camera configuration

        self.conf.update(specific_conf)

        self.band = self.conf['band']
        self.logger = logging.getLogger(f"deepspec.camera.{self.band}")
        init_log(self.logger, logging.DEBUG)

        self.enabled = bool(self.conf['enabled']) if 'enabled' in self.conf else False
        if not self.enabled:
            self.logger.info(f"Camera id={self.id} is disabled")
            return

        self.power = SwitchedPowerDevice(self.conf)
        if self.power.switch.is_off(self.power.outlet):
            self.power.switch.on(self.power.outlet)
        else:
            self.power.switch.cycle(self.power.outlet)

        boot_delay = self.conf['boot_delay'] if 'boot_delay' in self.conf else 20
        self.logger.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
        time.sleep(boot_delay)

        NetworkedDevice.__init__(self, self.conf)

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

        ipAddress = self.conf['network']['address']
        ret = ge.DisconnectCamera(addr=self.addr)
        self.logger.debug(f"ge.DisconnectCamera(addr={self.addr}) -> {ret}")
        ret = ge.DisconnectCameraServer(addr=self.addr)
        self.logger.debug(f"ge.DisconnectCameraServer(addr={self.addr}) -> {ret}")

        ret = ge.SetupCameraInterface(ge.connectionType_Ethernet, ipAddress=ipAddress, addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                              f"ipAddress={ipAddress}, addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")
            return
        self.logger.debug(f"OK: ge.SetupCameraInterface({ge.connectionType_Ethernet}, " +
                          f"ipAddress={ipAddress}, addr={self.addr}) (ret={ret}, msg='{ge.StatusMSG}')")

        # TODO: do we have to do this ONLY once ?
        ret = ge.ConnectToSingleCameraServer(addr=self.addr)
        if not ret:
            self.logger.error(f"Could not ge.ConnectToSingleCameraServer(addr={self.addr}) " +
                              f"(ret={ret}, msg='{ge.StatusMSG}')")
            return
        self.logger.debug(f"OK: ge.ConnectToSingleCameraServer(addr={self.addr}) " +
                          f"(ret={ret}, msg='{ge.StatusMSG}')")

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

        ret = ge.SetBitDepth(4)
        if not ret:
            self.logger.error(f"failed to set bit depth to 4 ({ret=})")
        self.logger.info(f"set bit depth to 4")

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

        self.timer = RepeatTimer(1, function=self.on_timer)
        self.timer.name = f'deepspec-camera-{self.band}-timer-thread'
        self.timer.start()

        self._initialized = True

    def set_led(self, on_off: bool):
        ret = ge.SetLEDStatus(on_off, addr=self.addr)
        if not ret:
            self.logger.error(f"could not set back LED to {'ON' if on_off else 'OFF'}")

    def __repr__(self):
        return (f"<Greateyes>(band={self.band}, id={self.id}, address='{self.address}', model='{self.model}', " +
                f"model_id='{self.model_id}', firmware_version={self.firmware_version})")

    def __del__(self):
        if self.addr is None:
            return
        ge.DisconnectCamera(addr=self.addr)
        ge.DisconnectCameraServer(addr=self.addr)

    def power_off(self):
        self.power.switch.off(self.power.outlet)

    def status(self) -> dict:
        ret = {
            'enabled': self.enabled,
            'band': self.band,
        }
        if self.enabled:
            ret['activities'] = self.activities
            ret['front_temperature'] = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
            ret['back_temperature'] = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.addr)
        return ret

    def cool_down(self):
        if ge.TemperatureControl_SetTemperature(temperature=self.target_cool_temp, addr=self.addr):
            self.start_activity(GreatEyesActivities.CoolingDown)

    def warm_up(self):
        if ge.TemperatureControl_SetTemperature(temperature=self.target_warm_temp, addr=self.addr):
            self.start_activity(GreatEyesActivities.WarmingUp)

    def startup(self):
        self.start_activity(GreatEyesActivities.StartingUp)
        self.cool_down()

    def shutdown(self):
        self.start_activity(GreatEyesActivities.ShuttingDown)
        self.warm_up()

    def set_parameters(self):
        readout_amplifiers = self.conf['readout_amplifiers'] if 'readout_amplifiers' in self.conf \
            else defaults['readout_amplifiers']

        ret = ge.SetupSensorOutputMode(readout_amplifiers, addr=self.addr)
        if ret:
            self.logger.info(f"set sensor output mode to {readout_amplifiers=}")
        else:
            self.logger.error(f"failed to set sensor output mode to {readout_amplifiers=} ({ret=})")

        self.x_binning = self.conf['x_binning'] if 'x_binning' in self.conf else defaults['x_binning']
        self.y_binning = self.conf['y_binning'] if 'y_binning' in self.conf else defaults['y_binning']
        ret = ge.SetBinningMode(self.x_binning, self.y_binning, addr=self.addr)
        if ret:
            self.logger.info(f"set binning to {self.x_binning=}, {self.y_binning=}")
        else:
            self.logger.error(f"failed to set binning to {self.x_binning=}, {self.y_binning=} ({ret=})")

        gain = self.conf['gain'] if 'gain' in self.conf else defaults['gain']
        ret = ge.SetupGain(gain, addr=self.addr)
        if ret:
            self.logger.info(f"set gain to {gain}")
        else:
            self.logger.error(f"failed to set gain to {gain} ({ret=})")

        bit_depth = self.conf['bit_depth'] if 'bit_depth' in self.conf else defaults['bit_depth']
        ret = ge.SetupGain(bit_depth, addr=self.addr)
        if ret:
            self.logger.info(f"set bit_depth to {bit_depth}")
        else:
            self.logger.error(f"failed to set bit_depth to {bit_depth} ({ret=})")

        ret = ge.SetReadOutSpeed(self.readout_speed, addr=self.addr)
        if ret:
            self.logger.info(f"set readout speed to {self.readout_speed}")
        else:
            self.logger.error(f"could not set readout speed to {self.readout_speed} ({ret=})")

    def expose(self, seconds: float | None = None):

        if not self.is_idle():
            if ge.DllIsBusy(addr=self.addr):
                self.logger.error(f"could not start exposure: ge.DllIsBusy()")
                return

            if self.is_active(GreatEyesActivities.CoolingDown):
                ret = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
                if ret == FAILED_TEMPERATURE:
                    self.logger.error(f"could not read sensor temperature ({ret=}")
                else:
                    delta_temp = abs(self.conf['target_cool_temp'] - ret)
                    self.logger.error(f"cannot expose while cooling down ({delta_temp=})")
                return

            if not self.is_idle():
                self.logger.error(f"camera is active ({self.activities=})")
                return

        if seconds is None:
            self.latest_exposure_time = self.conf['exposure'] if 'exposure' in self.conf else None
        if seconds is None:
            raise Exception(f"cannot figure out exposure time")
        self.latest_exposure_time = seconds

        ret = ge.SetExposure(self.latest_exposure_time, addr=self.addr)
        if not ret:
            self.logger.error(f"could not ge.SetExposure({seconds=}, addr={self.addr}) ({ret=})")
            return

        ret = ge.StartMeasurement_DynBitDepth(addr=self.addr)
        if ret:
            self.start_activity(GreatEyesActivities.Exposing)
            self.latest_exposure_utc_start = datetime.datetime.now(datetime.UTC)
            self.latest_exposure_local_start = datetime.datetime.now()
        else:
            self.logger.error(f"could not ge.StartMeasurement_DynBitDepth(addr={self.addr}) ({ret=})")

    def readout(self):
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
        # TODO: write to file (which, where?)
    
    def on_timer(self):
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
                if (now - self.timings[GreatEyesActivities.Exposing].start_time) > (self.latest_exposure_time + 10):
                    self.logger.error(f"exposure takes too long")
                    ret = ge.StopMeasurement(addr=self.addr)
                    if ret:
                        self.logger.error(f"could not ge.StopMeasurement(addr={self.addr}) ({ret=})")
                    else:
                        self.end_activity(GreatEyesActivities.Exposing)

        if self.is_active(GreatEyesActivities.CoolingDown):
            front_temp = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
            if front_temp == FAILED_TEMPERATURE:
                self.logger.error(f"failed reading sensor temperature")
            elif abs(front_temp - self.target_cool_temp) <= 1:
                self.end_activity(GreatEyesActivities.CoolingDown)
                ret = ge.TemperatureControl_SwitchOff(addr=self.addr)
                if ret:
                    self.logger.info(f"switched cooler OFF")
                else:
                    self.logger.error(f"could not switch cooler OFF (ret={ret})")
                if self.is_active(GreatEyesActivities.StartingUp):
                    self.end_activity(GreatEyesActivities.StartingUp)

        if self.is_active(GreatEyesActivities.WarmingUp):
            front_temp = ge.TemperatureControl_GetTemperature(thermistor=0, addr=self.addr)
            if front_temp == FAILED_TEMPERATURE:
                self.logger.error(f"failed reading sensor temperature")
            elif abs(front_temp - self.target_warm_temp) <= 1:
                self.end_activity(GreatEyesActivities.WarmingUp)
                ret = ge.TemperatureControl_SwitchOff(addr=self.addr)
                if ret:
                    self.logger.info(f"switched cooler OFF")
                else:
                    self.logger.error(f"could not switch cooler OFF (ret={ret})")
                if self.is_active(GreatEyesActivities.ShuttingDown):
                    self.end_activity(GreatEyesActivities.ShuttingDown)


def make_deepspec_cameras() -> List[GreatEyes]:

    configured_camera_ids = list(Config().toml['deepspec']['camera'].keys())
    cams: List[GreatEyes | None] = []

    for camera_id in configured_camera_ids:
        cam = GreatEyes(_id=camera_id)
        if cam.connected:
            cams.append(cam)
    return cams


if __name__ == "__main__":
    cameras: List[GreatEyes] = make_deepspec_cameras()

    for c in cameras:
        print(c)
        c.power_off()
