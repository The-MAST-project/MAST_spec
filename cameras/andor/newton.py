import datetime
import os
import threading
import time

import win32event
from pyAndorSDK2 import atmcd, atmcd_codes, atmcd_errors, atmcd_capabilities
import logging
from utils import init_log, Activities, RepeatTimer
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from enum import IntFlag, auto
from config.config import Config

from fastapi import APIRouter, Depends
from utils import BASE_SPEC_PATH, PrettyJSONResponse
from typing import Annotated

logger = logging.getLogger("mast.highspec.camera")
init_log(logger)

codes = atmcd_codes


class NewtonActivities(IntFlag):
    StartingUp = auto()
    ShuttingDown = auto()
    CoolingDown = auto()
    WarmingUp = auto()
    Exposing = auto()
    ReadingOut = auto()
    

class NewtonEMCCD(Activities, SwitchedPowerDevice):

    sdk: atmcd    
    serial_number: str
    x_pixels: int
    y_pixels: int
    _set_point_temp: float
    _ambient_temp: float
    timer: RepeatTimer

    SECONDS_BETWEEN_TEMP_LOGS = 30

    def __init__(self, conf: dict):
        self.conf = conf
        Activities.__init__(self)
        SwitchedPowerDevice.__init__(self, self.conf)

        self._initialized = False
        self.logger = logging.getLogger('mast.spec.highspec.camera')
        init_log(self.logger)

        self.SensorTemp = float('nan')
        self.TargetTemp = float('nan')
        self.AmbientTemp = float('nan')
        self.CoolerVolts = float('nan')
        self.last_temp_log: datetime = datetime.datetime.min

        self._set_point: int | None = None
        self.acquisition_mode: atmcd_capabilities.acquistionModes | None = None
        self.read_mode: int | None = None
        self.cooler_mode: int | None = None
        self.gain: int | None = None
        self.h_bin: int | None = None
        self.v_bin: int | None = None
        self.activate_cooler: bool | None = None
        self.exposure: float | None = None
        self._deleted = False
            
        self.sdk = atmcd()

        ret = self.sdk.Initialize(os.path.join(os.path.dirname(__file__), 'sdk'))
        if atmcd_errors.Error_Codes.DRV_SUCCESS != ret:
            self.logger.error(f"Could not initialize SDK (code={error_code(ret)})")
            return

        (ret, iSerialNumber) = self.sdk.GetCameraSerialNumber()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.serial_number = iSerialNumber
        else:
            self.logger.error(f"Could not get serial number (code={error_code(ret)})")
            self.sdk.ShutDown()
            return

        (ret, x_pixels, y_pixels) = self.sdk.GetDetector()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.x_pixels = x_pixels
            self.y_pixels = y_pixels
        else:
            self.logger.error(f"Could not GetDetector() (code={error_code(ret)})")
            self.sdk.ShutDown()
            return

        self.logger.info(f"Found camera SN: {self.serial_number}, {self.x_pixels=}, {self.y_pixels=}")
        self.set_modes()  # initial values

        event_handle = win32event.CreateEvent(None, 0, 0, None)
        ret = self.sdk.SetDriverEvent(event_handle.handle)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set event handler")
            self.event_handler = threading.Thread(name='event-handler-thread',
                                                  target=self.event_handler, args=(event_handle,))
            self.event_handler.start()
        else:
            self.logger.error(f"Could not set event handler (code={error_code(ret)})")

        # self.timer = RepeatTimer(2, function=self.on_timer)
        # self.timer.name = f'newton-timer-thread'
        # self.timer.start()

        self._initialized = True

    def event_handler(self, event_handle):
        while not self._deleted:
            result = win32event.WaitForSingleObject(event_handle, win32event.INFINITE)
            if result == win32event.WAIT_OBJECT_0:

                # when an event arrives, we get the status and temperature status and act accordingly
                (ret_code, status_code) = self.sdk.GetStatus()
                if ret_code == atmcd_errors.Error_Codes.DRV_SUCCESS:

                    if self.is_active(NewtonActivities.Exposing) and status_code == atmcd_errors.Error_Codes.DRV_IDLE:
                        self.end_activity(NewtonActivities.Exposing)
                        # TBD: readout ?!?

                    elif self.is_active(NewtonActivities.CoolingDown) or self.is_active(NewtonActivities.WarmingUp):
                        (temp_code, temp) = self.sdk.GetTemperatureF()
                        if temp_code == atmcd_errors.Error_Codes.DRV_TEMPERATURE_STABILIZED:
                            self.logger.info(f"Temperature has stabilized at {temp:.2f} degrees")

                            if self.is_active(NewtonActivities.CoolingDown):
                                self.end_activity(NewtonActivities.CoolingDown)
                            if self.is_active(NewtonActivities.StartingUp):
                                self.end_activity(NewtonActivities.StartingUp)

                            if self.is_active(NewtonActivities.WarmingUp):
                                self.end_activity(NewtonActivities.WarmingUp)
                            if self.is_active(NewtonActivities.ShuttingDown):
                                self.end_activity(NewtonActivities.ShuttingDown)
                        else:
                            self.logger.error(f"Could not GetTemperatureF() (code={error_code(temp_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ERROR_ACK:
                        self.logger.error(f"Driver cannot communicate with the camera " +
                                          f"(status_code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_BUFFER:
                        self.logger.error(f"Driver cannot read data at required rate " +
                                          f"(status_code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_DOWNFIFO_FULL:
                        self.logger.error(f"Driver cannot read data fast enough to prevent FIFO overflow " +
                                          f"(status_code={error_code(status_code)})")
                    else:
                        self.logger.error(f"Unhandled case: status_code={error_code(status_code)}")
                else:
                    self.logger.error(f"Could not GetStatus() (code={error_code(ret_code)})")

                win32event.ResetEvent(event_handle)
                # self.sdk.SetDriverEvent(0)
            else:
                self.logger.error(f"failed to win32event.WaitForSingleObject() ({result=}")

    def set_modes(self,
                  exposure: float | None = None,
                  acquisition_mode: atmcd_capabilities.acquistionModes | None = None,
                  read_mode=None,
                  set_point: int | None = None,
                  cooler_mode=None,
                  activate_cooler: bool | None = None,
                  gain: int | None = None,
                  h_bin: int | None = None,
                  v_bin: int | None = None,
                  save: bool = False):

        conf = self.conf
        self.exposure = exposure if exposure is not None else conf['exposure'] if 'exposure' in conf else 10

        self.acquisition_mode = acquisition_mode if acquisition_mode is not None else conf['acquisition-mode'] if\
            'acquisition-mode' in conf else atmcd_capabilities.acquistionModes.AC_ACQMODE_SINGLE

        self.read_mode = read_mode if read_mode is not None else conf['read-mode'] if 'read-mode' in conf \
            else atmcd_capabilities.readmodes.AC_READMODE_FULLIMAGE

        self._set_point = set_point if set_point is not None else conf['set-point'] if 'set-point' in conf else -60

        self.cooler_mode = cooler_mode if cooler_mode is not None else conf['cooler-mode'] if 'cooler-mode' in conf \
            else 0

        self.gain = gain if gain is not None else conf['gain'] if 'gain' in conf else 200
        self.h_bin = h_bin if h_bin is not None else conf['h-bin'] if 'h-bin' in conf else 1
        self.v_bin = v_bin if v_bin is not None else conf['v-bin'] if 'v-bin' in conf else 1
        self.activate_cooler = activate_cooler if activate_cooler is not None else conf['activate-cooler'] if 'activate-cooler' in conf else False

        ret = self.sdk.SetAcquisitionMode(self.acquisition_mode)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set acquisition mode to {atmcd_capabilities.acquistionModes(self.acquisition_mode)}")
        else:
            self.logger.error(f"Could not set acquisition mode to SINGLE_SCAN (code={error_code(ret)})")

        ret = self.sdk.SetCoolerMode(self.cooler_mode)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set cooler mode to {self.cooler_mode}")
        else:
            self.logger.error(f"Could not set cooler mode to {self.cooler_mode} (code={error_code(ret)})")

        ret = self.sdk.SetReadMode(codes.Read_Mode.IMAGE)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set read mode to {codes.Read_Mode.IMAGE}")
        else:
            self.logger.error(f"Could not set acquisition mode to {codes.Read_Mode.IMAGE} (code={error_code(ret)})")

        ret = self.sdk.SetTriggerMode(codes.Trigger_Mode.INTERNAL)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set trigger mode to {codes.Read_Mode.IMAGE}")
        else:
            self.logger.error(f"Could not set trigger mode to {codes.Trigger_Mode.INTERNAL} (code={error_code(ret)})")

        ret = self.sdk.SetImage(self.h_bin, self.v_bin, 1, self.x_pixels, 1, self.y_pixels)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set image to ({self.h_bin=}, {self.v_bin=}, 1, {self.x_pixels=}, 1, {self.y_pixels=})")
        else:
            self.logger.error(f"Could not set image (code={error_code(ret)})")

        if 0 <= self.gain <= 255:
            ret = self.sdk.SetEMGainMode(0)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.logger.info(f"Set EMGainMode to 0")
            else:
                self.logger.error(f"Could not set EMGainMode to 0, (code={error_code(ret)})")
        elif 256 <= self.gain <= 4095:
            ret = self.sdk.SetEMAdvanced(1)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                ret = self.sdk.SetEMGainMode(1)
                if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                    self.logger.info(f"Set EMGainMode to 1")
                else:
                    self.logger.error(f"Could not set EMGainMode to 1 (code={error_code(ret)})")
            else:
                self.logger.error(f"Could not set EMAdvanced to 1 (code={error_code(ret)})")
        else:
            raise Exception(f"Cannot set gain to {self.gain} (allowed: 0 >= gain <= 4095)")

        ret = self.sdk.SetEMCCDGain(self.gain)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set gain to {self.gain}")
        else:
            self.logger.error(f"Could not set gain to {self.gain} (code={error_code(ret)})")

        ret = self.sdk.SetTemperature(self._set_point)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set set-point to {self._set_point:.2f}")
        else:
            self.logger.error(f"Could not set set-point to {self._set_point:.2f}")

        ret = self.sdk.SetExposureTime(self.exposure)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set exposure time to {self.exposure}")
        else:
            self.logger.error(f"Could not set exposure time to {self.exposure} (code={error_code(ret)})")

        if self.activate_cooler:
            self.start_activity(NewtonActivities.CoolingDown)
            self.turn_cooler(True)
        else:
            self.turn_cooler(False)

        if save:
            # TODO: update conf and toml.save it
            self.logger.error(f"save is not implemented yet!")

    @property
    def set_point(self):
        return self._set_point
    
    @set_point.setter
    def set_point(self, value: float):
        self._set_point = value

    def turn_cooler(self, on_off: bool):

        ret = self.sdk.CoolerON() if on_off else self.sdk.CoolerOFF()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"turned the cooler {'ON' if on_off else 'OFF'}")
        else:
            self.logger.error(f"Could not turn the Cooler {'ON' if on_off else 'OFF'} (code={error_code(ret)})")
    
    def expose(self, seconds: float | None = None):
        if not self._initialized:
            raise Exception("SDK not initialized")

        if seconds is not None:
            ret = self.sdk.SetExposureTime(seconds)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.logger.info(f"Set exposure time to {seconds=:.2f}")
            else:
                self.logger.error(f"Could not set exposure time to {seconds=:.2f} (code={error_code(ret)})")
                return

        ret = self.sdk.StartAcquisition()
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"Could not StartAcquisition() (code={error_code(ret)})")
            return

        self.start_activity(NewtonActivities.Exposing)
    
    def startup(self):
        self.start_activity(NewtonActivities.StartingUp)
        self.start_activity(NewtonActivities.CoolingDown)
        self.turn_cooler(True)
    
    def shutdown(self):
        self.start_activity(NewtonActivities.ShuttingDown)
        self.start_activity(NewtonActivities.WarmingUp)
        self.turn_cooler(False)  # or do we need to a controlled warmup by setting the set-point?
        # TBD:
        # - do we need to wait for the temperature to stabilize?
        # - do we power off after stabilization?
    
    def abort(self):
        if self.is_active(NewtonActivities.Exposing):
            ret = self.sdk.AbortAcquisition()
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.end_activity(NewtonActivities.Exposing)
                self.logger.debug("Aborted acquisition")
            else:
                self.logger.error(f"Could not AbortAcquisition() (code={error_code(ret)})")

    def get_temperature(self):
        if not self._initialized:
            raise Exception("SDK not initialized")

        (ret, temp) = self.sdk.GetTemperatureF()
        if ret == atmcd_errors.Error_Codes.DRV_TEMP_STABILIZED:
            return temp
        else:
            self.logger.error(f"Could not GetTemperatureF() (code={error_code(ret)})")
            return {
                'Error': f"Could not GetTemperatureF() (code={error_code(ret)})"
            }

    def __del__(self):
        self._deleted = True
        self.sdk.SetDriverEvent(0)
        self.sdk.ShutDown()


def error_code(code) -> str:
    return atmcd_errors.Error_Codes(code).__repr__()

#
# FastAPI stuff
#


def show_camera():
    return {
        'SN': camera.serial_number,
        'x_pixels': camera.x_pixels,
        'y_pixels': camera.y_pixels,
        'horizontal_binning': camera.h_bin,
        'vertical_binning': camera.v_bin,
        'acquisition_mode': camera.acquisition_mode,
        'set_point': camera.set_point,
        'read_mode': camera.read_mode,
        'gain': camera.gain,
        'exposure': camera.exposure,

        'activate_cooler': camera.activate_cooler,
        'cooler_mode': camera.cooler_mode,

        'power': {
            'switch': camera.conf['power']['switch'],
            'outlet': camera.conf['power']['outlet'],
            'state':  'ON' if camera.is_on() else 'OFF',
        },
    }


def take_exposure(seconds: float):
    camera.expose(seconds)


def camera_status():
    return {
        'activities': camera.activities,
        'temperature': camera.get_temperature(),
    }


def camera_modes() -> dict:
    return {
        'exposure': camera.exposure,
        'acquisition_mode': camera.acquisition_mode,
        'read_mode': camera.read_mode,
        'horizontal_binning': camera.h_bin,
        'vertical_binning': camera.v_bin,
        'gain': camera.gain,
        'set_point': camera.set_point,
        'save': False,
    }


def set_camera_modes(modes: dict = Depends(camera_modes)):
    camera.set_modes(
        exposure=modes['exposure'],
        acquisition_mode=modes['acquisition-mode'],
        read_mode=modes['read-mode'],
        set_point=modes['set-point'],
        gain=modes['gain'],
        h_bin=modes['horizontal-binning'],
        v_bin=modes['vertical-binning'],
        save=modes['save'],
    )


base_path = BASE_SPEC_PATH + '/highspec/camera'
tag = 'HighSpec Camera'
router = APIRouter()

router.add_api_route(base_path, tags=[tag], endpoint=show_camera, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=take_exposure, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=camera_status, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/set-modes', tags=[tag], endpoint=set_camera_modes,
                     response_class=PrettyJSONResponse)

camera = NewtonEMCCD(Config().toml['highspec']['camera'])

if __name__ == '__main__':

    camera.startup()
    while camera.is_active(NewtonActivities.StartingUp):
        camera.logger.debug(f"waiting for NewtonActivities.StartingUp to end ...")
        time.sleep(5)

    camera.expose()
    while camera.is_active(NewtonActivities.Exposing):
        camera.logger.debug(f"waiting for NewtonActivities.Exposing to end ...")
        time.sleep(5)

    print("done")

