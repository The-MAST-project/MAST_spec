import datetime
import os
import threading
import time
from typing import List

import win32event
# from pyAndorSDK2 import atmcd, atmcd_codes, atmcd_errors, atmcd_capabilities
import logging

from utils import init_log, PathMaker
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from enum import IntFlag, auto, Enum
from config import Config

from fastapi import APIRouter, Query
from utils import BASE_SPEC_PATH, Component

logger = logging.getLogger("mast.highspec.camera")
init_log(logger)

from pyAndorSDK2 import atmcd_codes, atmcd_errors, atmcd_capabilities
codes = atmcd_codes


def current_exposure():
    return camera.exposure


def current_acquisition_mode():
    return camera.acquisition_mode


def current_read_mode():
    return camera.read_mode


def current_set_point():
    return camera.set_point


def current_cooler_mode():
    return camera.cooler_mode


def current_activate_cooler():
    return camera.activate_cooler


def current_gain():
    return camera.gain


def current_horizontal_binning():
    return camera.horizontal_binning


def current_vertical_binning():
    return camera.vertical_binning


class NewtonActivities(IntFlag):
    StartingUp = auto()
    ShuttingDown = auto()
    CoolingDown = auto()
    WarmingUp = auto()
    Exposing = auto()
    ReadingOut = auto()
    
    
class AcquisitionMode(Enum):
    SINGLE_SCAN = 1
    ACCUMULATE = 2
    KINETICS = 3
    FAST_KINETICS = 4
    RUN_TILL_ABORT = 5


acquisition_modes = Enum('AcquisitionModes', list(zip(
    list(AcquisitionMode.__members__), list(AcquisitionMode.__members__))))


class ReadMode(Enum):
    FULL_VERTICAL_BINNING = 0
    MULTI_TRACK = 1
    RANDOM_TRACK = 2
    SINGLE_TRACK = 3
    IMAGE = 4


read_modes = Enum('ReadModes', list(zip(
    list(ReadMode.__members__), list(ReadMode.__members__))))


class CoolerMode(Enum):
    RETURN_TO_AMBIENT = 0,
    MAINTAIN_CURRENT_TEMP = 1,


cooler_modes = Enum('CoolerModes', list(zip(
    list(CoolerMode.__members__), list(CoolerMode.__members__)
)))

defaults = {
    'exposure': 10,
    'acquisition-mode': AcquisitionMode.SINGLE_SCAN,
    'read-mode': ReadMode.IMAGE,
    'set-point': -60,
    'gain': 200,
    'horizontal-binning': 1,
    'vertical-binning': 1,
    'cooler-mode': CoolerMode.RETURN_TO_AMBIENT,
    'activate-cooler': True,
}


class NewtonEMCCD(Component, SwitchedPowerDevice):

    SECONDS_BETWEEN_TEMP_LOGS = 30
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(NewtonEMCCD, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.conf = Config().toml['highspec']['camera']
        Component.__init__(self)
        self._name = 'highspec'

        self.detected = False

        # NOTE: The power to this camera is switched on by spec.startup()
        self.power = SwitchedPowerDevice(self.conf)

        self._initialized = False
        self.logger = logging.getLogger('mast.spec.highspec.camera')
        init_log(self.logger)

        self.SensorTemp = float('nan')
        self.TargetTemp = float('nan')
        self.AmbientTemp = float('nan')
        self.CoolerVolts = float('nan')
        self.last_temp_log: datetime = datetime.datetime.min

        self._set_point: int | None = None
        self.acquisition_mode: AcquisitionMode | None = None
        self.read_mode: ReadMode | None = None
        self.cooler_mode: int | None = None
        self.gain: int | None = None
        self.horizontal_binning: int | None = None
        self.vertical_binning: int | None = None
        self.activate_cooler: bool | None = None
        self.exposure: float | None = None

        if not self.power.switch.detected:
            return

        from pyAndorSDK2 import atmcd
        self.sdk = atmcd()

        ret = self.sdk.Initialize(os.path.join(os.path.dirname(__file__), 'sdk', 'pyAndorSDK2', 'pyAndorSDK2'))
        if atmcd_errors.Error_Codes.DRV_SUCCESS != ret:
            self.logger.error(f"Could not initialize SDK (code={error_code(ret)})")
            return

        (ret, iSerialNumber) = self.sdk.GetCameraSerialNumber()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.serial_number = iSerialNumber
            self.detected = True
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

        self.min_temp: float | None = None
        self.max_temp: float | None = None
        (ret, min_temp, max_temp) = self.sdk.GetTemperatureRange()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.min_temp = min_temp
            self.max_temp = max_temp

        self.acquisition: str | None = None

        self.logger.info(f"Found camera SN: {self.serial_number}, {self.x_pixels=}, {self.y_pixels=}")
        self.set_modes()  # initial values

        event_handle = win32event.CreateEvent(None, 0, 0, None)
        ret = self.sdk.SetDriverEvent(event_handle.handle)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set event handler")
            self._terminated = False
            self.event_handler_thread = threading.Thread(name='event-handler-thread',
                                                         target=self.event_handler, args=(event_handle,))
            self.event_handler_thread.start()
        else:
            self.logger.error(f"Could not set event handler (code={error_code(ret)})")

        self.start_cooldown()

        self._initialized = True

    def start_cooldown(self):
        self.turn_cooler(True)
        target_temp = self.set_point
        ret = self.sdk.SetTemperature(target_temp)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"failed to set temperature to {target_temp} degrees (code={error_code(ret)})")
            return
        self.start_activity(NewtonActivities.CoolingDown)

    def start_warmup(self):
        self.turn_cooler(True)
        target_temp = self.max_temp
        ret = self.sdk.SetTemperature(target_temp)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"failed to set temperature to {target_temp} degrees (code={error_code(ret)})")
            return
        self.start_activity(NewtonActivities.WarmingUp)

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str):
        self._name = value

    @property
    def operational(self) -> bool:
        return (self.power.switch.detected and self.detected and not
        (self.is_active(NewtonActivities.CoolingDown) or self.is_active(NewtonActivities.WarmingUp)))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = 'highspec'
        if not self.detected:
            ret.append(f"{label} camera not detected")
        if self.is_active(NewtonActivities.CoolingDown):
            ret.append(f"{label} camera is CoolingDown")
        if self.is_active(NewtonActivities.WarmingUp):
            ret.append(f"{label} camera is WarmingUp")
        return ret

    def event_handler(self, event_handle):
        """
        Handles Win32 events from the SDK
        :param event_handle:
        :return:
        """
        while not self._terminated:
            result = win32event.WaitForSingleObject(event_handle, win32event.INFINITE)
            if result == win32event.WAIT_OBJECT_0:

                # when an event arrives, we get the status and temperature status and act accordingly
                (ret_code, status_code) = self.sdk.GetStatus()
                if ret_code == atmcd_errors.Error_Codes.DRV_SUCCESS:

                    if self.is_active(NewtonActivities.Exposing) and status_code == atmcd_errors.Error_Codes.DRV_IDLE:
                        self.end_activity(NewtonActivities.Exposing)
                        threading.Thread(name=f"highspec-readout", target=self.readout).start()

                    elif self.is_active(NewtonActivities.CoolingDown) or self.is_active(NewtonActivities.WarmingUp):
                        (temp_code, temp) = self.sdk.GetTemperatureF()
                        if temp_code == atmcd_errors.Error_Codes.DRV_TEMPERATURE_STABILIZED:
                            self.logger.info(f"Temperature has stabilized at {temp:.2f} degrees")

                            if self.is_active(NewtonActivities.CoolingDown):
                                self.end_activity(NewtonActivities.CoolingDown)
                                if self.is_active(NewtonActivities.StartingUp):
                                    self.end_activity(NewtonActivities.StartingUp)

                            power_off = False
                            if self.is_active(NewtonActivities.WarmingUp):
                                self.end_activity(NewtonActivities.WarmingUp)
                                if self.is_active(NewtonActivities.ShuttingDown):
                                    self.end_activity(NewtonActivities.ShuttingDown)
                                    ret = self.sdk.CoolerOFF()
                                    if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
                                        self.logger.error(f"could not turn cooler OFF (code={error_code(ret)}")
                                    power_off = True
                            if power_off:
                                self.power.switch.off(self.power.outlet)
                        else:
                            self.logger.error(f"Could not GetTemperatureF() (code={error_code(temp_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ERROR_ACK:
                        self.logger.error(f"Driver cannot communicate with the camera " +
                                          f"(code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_BUFFER:
                        self.logger.error(f"Driver cannot read data at required rate " +
                                          f"(code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_DOWNFIFO_FULL:
                        self.logger.error(f"Driver cannot read data fast enough to prevent FIFO overflow " +
                                          f"(code={error_code(status_code)})")
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
                  acquisition_mode: AcquisitionMode | None = None,
                  read_mode: ReadMode | None = None,
                  set_point: float | None = None,
                  cooler_mode: CoolerMode | None = None,
                  activate_cooler: bool | None = None,
                  gain: int | None = None,
                  horizontal_binning: int | None = None,
                  vertical_binning: int | None = None,
                  save: bool = False):

        conf = self.conf
        self.exposure = exposure if exposure is not None else conf['exposure'] \
            if 'exposure' in conf else defaults['exposure']

        self.acquisition_mode = acquisition_mode if acquisition_mode is not None else conf['acquisition-mode'] if\
            'acquisition-mode' in conf else defaults['acquisition-mode']

        self.read_mode = read_mode if read_mode is not None else conf['read-mode'] if 'read-mode' in conf \
            else defaults['read-mode']

        self._set_point = set_point if set_point is not None else conf['set-point'] \
            if 'set-point' in conf else defaults['set-point']

        self.cooler_mode = cooler_mode if cooler_mode is not None else conf['cooler-mode'] if 'cooler-mode' in conf \
            else defaults['cooler-mode']

        self.gain = gain if gain is not None else conf['gain'] if 'gain' in conf else defaults['gain']

        self.horizontal_binning = horizontal_binning if horizontal_binning is not None else conf['h-bin'] \
            if 'h-bin' in conf else defaults['horizontal-binning']

        self.vertical_binning = vertical_binning if vertical_binning is not None else conf['v-bin'] \
            if 'v-bin' in conf else defaults['vertical-binning']

        (ret, max_horizontal_binning) = self.sdk.GetMaximumBinning(self.read_mode.value, 0)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"could not sdk.GetMaximumBinning({self.read_mode.value}, 0) (ret={ret}")
        elif self.horizontal_binning > max_horizontal_binning:
            return {'error':
                    f"Horizontal binning for ReadMode {self.read_mode.name} cannot exceed {max_horizontal_binning}"}

        (ret, max_vertical_binning) = self.sdk.GetMaximumBinning(self.read_mode.value, 1)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"could not sdk.GetMaximumBinning({self.read_mode.value}, 1) (ret={ret}")
        elif self.vertical_binning > max_vertical_binning:
            return {'error':
                    f"Vertical binning for ReadMode {self.read_mode.name} cannot exceed {max_vertical_binning}"}

        self.activate_cooler = activate_cooler if activate_cooler is not None else conf['activate-cooler'] \
            if 'activate-cooler' in conf else defaults['activate-cooler']

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

        ret = self.sdk.SetImage(self.horizontal_binning, self.vertical_binning,
                                1, self.x_pixels, 1, self.y_pixels)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"Set image to ({self.horizontal_binning=}, {self.vertical_binning=}, " +
                             f"1, {self.x_pixels=}, 1, {self.y_pixels=})")
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
        if not self.detected:
            self.logger.error(f"camera not detected")
            return

        ret = self.sdk.CoolerON() if on_off else self.sdk.CoolerOFF()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"turned the cooler {'ON' if on_off else 'OFF'}")
        else:
            self.logger.error(f"Could not turn the Cooler {'ON' if on_off else 'OFF'} (code={error_code(ret)})")
    
    def expose(self, seconds: float | None = None, acquisition: str | None = None):
        """
        Starts an exposure.
        :param seconds: exposure duration (seconds)
        :param acquisition: if given, a directory where to store the image
        :return:
        """
        if not self.detected:
            self.logger.error(f"camera not detected")
            return

        if not self._initialized:
            self.logger.error(f"not initialized")
            return

        if seconds is not None:
            ret = self.sdk.SetExposureTime(seconds)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.logger.info(f"Set exposure time to {seconds=:.2f}")
            else:
                self.logger.error(f"Could not set exposure time to {seconds=:.2f} (code={error_code(ret)})")
                return

        if acquisition:
            self.acquisition = acquisition

        ret = self.sdk.StartAcquisition()
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"Could not StartAcquisition() (code={error_code(ret)})")
            return
        self.logger.info(f"started exposure with sdk.StartAcquisition()")

        self.start_activity(NewtonActivities.Exposing)

    def readout(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        filename = PathMaker().make_exposure_file_name(camera='highspec', acquisition=self.acquisition) + '.fits'
        ret = self.sdk.SaveAsFITS(filename, typ=1)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"failed sdk.SaveAsFITS({filename}, typ=1) (ret={ret}")
        self.logger.info(f"saved exposure in '{filename}")
        self.end_activity(NewtonActivities.Exposing)

    def startup(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        self.start_activity(NewtonActivities.StartingUp)
        self.start_activity(NewtonActivities.CoolingDown)
        self.turn_cooler(True)
    
    def shutdown(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        self.start_activity(NewtonActivities.ShuttingDown)
        self.start_warmup()
    
    def abort(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        if self.is_active(NewtonActivities.Exposing):
            ret = self.sdk.AbortAcquisition()
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.end_activity(NewtonActivities.Exposing)
                self.logger.debug("Aborted acquisition")
            else:
                self.logger.error(f"Could not AbortAcquisition() (code={error_code(ret)})")

    def get_temperature(self) -> float | None:
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        if not self._initialized:
            raise Exception("SDK not initialized")

        (ret, temp) = self.sdk.GetTemperatureF()
        if ret == atmcd_errors.Error_Codes.DRV_TEMP_STABILIZED:
            return temp
        else:
            self.logger.error(f"Could not GetTemperatureF() (code={error_code(ret)})")
            return None

    def __del__(self):
        self._terminated = True
        if self.detected:
            self.sdk.SetDriverEvent(0)
            self.sdk.ShutDown()

    def status(self):
        ret = {
            'detected': self.detected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
        }
        if self.detected:
            ret['activities'] = self.activities
            ret['activities_verbal'] = self.activities.__repr__()
            ret['idle'] = self.is_idle()
            ret['temperature'] = self.get_temperature()

        return ret

    def can_expose(self) -> List[str]:
        ret = []
        if not self.detected:
            ret.append('not-detected')
        if not self._initialized:
            ret.append('not initialized')
        temp = self.get_temperature()
        if temp > self.TargetTemp:
            ret.append(f'temperature ({temp=} above {self.TargetTemp}')
        return ret


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
        'horizontal_binning': camera.horizontal_binning,
        'vertical_binning': camera.vertical_binning,
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
    return camera.status()


def camera_modes() -> dict:
    return {
        'exposure': camera.exposure,
        'acquisition_mode': camera.acquisition_mode,
        'read_mode': camera.read_mode,
        'horizontal_binning': camera.horizontal_binning,
        'vertical_binning': camera.vertical_binning,
        'gain': camera.gain,
        'set_point': camera.set_point,
        'save': False,
    }


def set_camera_modes(
        exposure: float = Query(description="Exposure length (seconds)", default=defaults['exposure']),
        acquisition_mode: acquisition_modes = Query(description='Select a pre-defined acquisition modes',
                                                    default=defaults['acquisition-mode'].name),
        read_mode: read_modes = Query(description='Select a pre-defined read mode',
                                      default=defaults['read-mode'].name),
        set_point: float = Query(default=defaults['set-point'], description='Target temperature'),
        gain: int = Query(default=defaults['gain'], ge=1, le=4095),
        horizontal_binning: int = Query(default=defaults['horizontal-binning'], ge=1, le=1600),
        vertical_binning: int = Query(default=defaults['vertical-binning'], ge=1, le=400),
        activate_cooler: bool = Query(default=defaults['activate-cooler']),
        cooler_mode: cooler_modes = Query(default=defaults['cooler-mode'].name,
                                          description='What to do about temperature at shutdown?'),
        save: bool = Query(description='Save these settings as defaults?', default=False),
):
    camera.set_modes(
        exposure=exposure,
        acquisition_mode=getattr(AcquisitionMode, acquisition_mode.value),
        read_mode=getattr(ReadMode, read_mode.value),
        set_point=set_point,
        gain=gain,
        horizontal_binning=horizontal_binning,
        vertical_binning=vertical_binning,
        activate_cooler=activate_cooler,
        cooler_mode=getattr(CoolerMode, cooler_mode.value),
        save=save,
    )


def startup():
    camera.startup()


def shutdown():
    camera.shutdown()


def abort():
    camera.abort()


base_path = BASE_SPEC_PATH + 'highspec/camera'
tag = 'HighSpec Camera'
router = APIRouter()

router.add_api_route(base_path, tags=[tag], endpoint=show_camera)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=take_exposure)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=camera_status)
router.add_api_route(base_path + '/set-modes', tags=[tag], endpoint=set_camera_modes)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=abort)


camera = NewtonEMCCD()


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
