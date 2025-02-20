import datetime
import os
import threading
import time
from sys import settrace
from typing import List, Callable,Optional

import win32event
from pyAndorSDK2 import atmcd, atmcd_codes, atmcd_errors, atmcd_capabilities, CameraCapabilities
import logging

from common.mast_logging import init_log
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from enum import IntFlag, auto, Enum
from common.config import Config
from common.spec import SpecExposureSettings
from common.filer import Filer

from fastapi import APIRouter, Query
from common.utils import BASE_SPEC_PATH, Component
from common.models.newton import NewtonCameraSettingsModel

logger = logging.getLogger("mast.highspec.camera")
init_log(logger)

codes = atmcd_codes


def current_exposure():
    return camera.exposure_duration


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
    return camera.em_gain


def current_horizontal_binning():
    return camera.horizontal_binning


def current_vertical_binning():
    return camera.vertical_binning


class NewtonActivities(IntFlag):
    StartingUp = auto()
    ShuttingDown = auto()
    CoolingDown = auto()
    WarmingUp = auto()
    Acquiring = auto()
    Exposing = auto()
    ReadingOut = auto()
    Saving = auto()
    SettingParameters = auto()
    
    
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
    'exposure_duration': 10,
    'acquisition-mode': AcquisitionMode.SINGLE_SCAN,
    'read-mode': ReadMode.IMAGE,
    'set-point': -60,
    'em_gain': 200,
    'horizontal-binning': 1,
    'vertical-binning': 1,
    'cooler-mode': CoolerMode.RETURN_TO_AMBIENT,
    'activate-cooler': True,
}

class Capabilities:
    ulAcqModes: atmcd_capabilities.acquistionModes
    ulCameraType: atmcd_capabilities.cameratype
    ulEMGainCapability: atmcd_capabilities.EmGainModes
    ulFTReadModes: atmcd_capabilities.readmodes
    ulFeatures: atmcd_capabilities.Features
    ulFeatures2: atmcd_capabilities.Features2
    ulGetFunctions: atmcd_capabilities.GetFunctions
    ulPCICcard: int
    ulPixelModes: atmcd_capabilities.PixelModes
    ulReadModes: atmcd_capabilities.readmodes
    ulSetFunctions: atmcd_capabilities.SetFunctions
    ulSize: int
    ulTriggerModes: atmcd_capabilities.triggermodes



class NewtonEMCCD(Component, SwitchedOutlet):

    SECONDS_BETWEEN_TEMP_LOGS = 30
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(NewtonEMCCD, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.conf = Config().get_specs()['highspec']
        Component.__init__(self)
        self._name = 'highspec'

        self._detected = False

        # NOTE: The power to this camera is switched on by spec.startup()
        SwitchedOutlet.__init__(self, outlet_name='Highspec', domain=OutletDomain.Spec)
        if self.power_switch.detected and not self.is_on():
            self.power_on()

        self._initialized = False
        self.logger = logging.getLogger('mast.spec.highspec.camera')
        init_log(self.logger)

        self.SensorTemp = float('nan')
        self.TargetTemp = float('nan')
        self.AmbientTemp = float('nan')
        self.CoolerVolts = float('nan')
        self.last_temp_log: datetime = datetime.datetime.min

        self._set_point: Optional[int] = None
        self.acquisition_mode: Optional[AcquisitionMode] = None
        self.read_mode: ReadMode | None = None
        self.cooler_mode: Optional[int] = None
        self.em_gain: Optional[int] = None
        self.horizontal_binning: Optional[int] = None
        self.vertical_binning: Optional[int] = None
        self.activate_cooler:Optional[bool] = None
        self.exposure_duration: Optional[float] = None

        if not self.power_switch.detected:
            return

        self.errors = []
        self.sdk = atmcd()
        ret = self.sdk.Initialize("")
        if atmcd_errors.Error_Codes.DRV_SUCCESS != ret:
            self.logger.error(f"Could not initialize SDK (code={error_code(ret)})")
            return

        self.parse_camera_capabilities()

        (ret, serial_number) = self.sdk.GetCameraSerialNumber()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.serial_number = serial_number
            self._detected = True
        else:
            self.logger.error(f"Could not get serial number (code={error_code(ret)})")
            self.sdk.ShutDown()
            return

        (ret, capabilities) = self.sdk.GetCapabilities()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.caps: Capabilities = capabilities
        else:
            self.logger.error(f"Could not GetCapabilities() (code={error_code(ret)})")

        if not self.caps.ulCameraType & atmcd_capabilities.cameratype.AC_CAMERATYPE_NEWTON:
            raise Exception(f"the camera is not a NEWTON")

        self.logger.info(f"found a NEWTON camera, SN: {self.serial_number}")
        if not self.caps.ulSetFunctions & atmcd_capabilities.SetFunctions.AC_SETFUNCTION_EMADVANCED:
            self.logger.warn(f"no AC_SETFUNCTION_EMADVANCED capability")
        if not self.caps.ulSetFunctions & atmcd_capabilities.SetFunctions.AC_SETFUNCTION_EMCCDGAIN:
            self.logger.warn(f"no AC_SETFUNCTION_EMCCDGAIN capability")

        (ret, x_pixels, y_pixels) = self.sdk.GetDetector()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.x_pixels = x_pixels
            self.y_pixels = y_pixels
        else:
            self.logger.error(f"Could not GetDetector() (code={error_code(ret)})")
            self.sdk.ShutDown()
            return
        self.logger.info(f"detector size: {self.x_pixels}x{self.y_pixels}")

        self.min_temp: float | None = None
        self.max_temp: float | None = None
        (ret, min_temp, max_temp) = self.sdk.GetTemperatureRange()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.min_temp = min_temp
            self.max_temp = max_temp
            self.logger.info(f"got temperature range: {self.min_temp}, {self.max_temp}")
        else:
            self.logger.error(f"could not GetTemperatureRange() (code={error_code(ret)})")

        (ret, n_pre_amp_gains) = self.sdk.GetNumberPreAmpGains()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.n_pre_amp_gains = n_pre_amp_gains
            self.logger.info(f"got n_gains: {self.n_pre_amp_gains}")
        else:
            self.logger.error(f"could not GetNumberPreAmpGains() (code={error_code(ret)})")

        (ret, max_exposure_time) = self.sdk.GetMaximumExposure()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.max_exposure_time = max_exposure_time
            self.logger.info(f"got max exposure_duration time: {self.max_exposure_time}")
        else:
            self.logger.error(f"could not GetMaximumExposure() (code={error_code(ret)})")

        self._apply_setting(self.sdk.SetOutputAmplifier, 0)
        # self._apply_setting(self.sdk.SetEMAdvanced, 1)
        # self._apply_setting(self.sdk.SetEMGainMode, 1)
        (ret, low, high) = self.sdk.GetEMGainRange()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.lowest_em_gain = low
            self.highest_em_gain = high
            self.logger.info(f"got em gain range: {self.lowest_em_gain}, {self.highest_em_gain}")
        else:
            self.logger.error(f"could not GetEMGainRange() ({ret=})")

        # TODO: check if our camera can generate ESD events

        self.latest_exposure_settings: SpecExposureSettings | None = None

        default_camera_settings: NewtonCameraSettingsModel = (
            NewtonCameraSettingsModel(**Config().get_specs()['highspec']['settings']))
        self.latest_camera_settings: NewtonCameraSettingsModel | None = None
        self.apply_settings(default_camera_settings)

        driver_event_handle = win32event.CreateEvent(None, 0, 0, None)
        ret = self.sdk.SetDriverEvent(driver_event_handle.handle)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set driver event handler")
            self._terminated = False
            self.driver_event_handler_thread = threading.Thread(name='event-handler-thread',
                                                                target=self.driver_event_handler, args=(driver_event_handle,))
            self.driver_event_handler_thread.start()
        else:
            self.logger.error(f"Could not set driver event handler (code={error_code(ret)})")

        self.start_cooldown()
        self._was_shut_down = False

        self._initialized = True

    def append_error(self, err: str):
        self.errors.append(err)
        self.logger.error(err)

    def parse_camera_capabilities(self):
        """
        Parse and print capabilities returned by sdk GetCapabilities()
        :return:
        """
        helper = CameraCapabilities.CapabilityHelper(self.sdk)
        print('capabilities')
        helper.print_all()

    def start_cooldown(self):
        self.turn_cooler(True)
        target_temp = self.latest_camera_settings.temperature.set_point
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
    def detected(self) -> bool:
        return self._detected

    @property
    def connected(self) -> bool:
        return self.detected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @property
    def operational(self) -> bool:
        return (self.power_switch.detected and self.is_on() and self.detected and not
            (self.is_active(NewtonActivities.CoolingDown) or self.is_active(NewtonActivities.WarmingUp)))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = 'highspec:'
        if not self.power_switch.detected:
            ret.append(f"{label} {self.power_switch} not detected")
        elif self.is_off():
            ret.append(f"{label} {self.power_switch}:{self.outlet_name} is OFF")
        else:
            if not self.detected:
                ret.append(f"{label} camera not detected")
            if self.is_active(NewtonActivities.CoolingDown):
                ret.append(f"{label} camera is CoolingDown")
            if self.is_active(NewtonActivities.WarmingUp):
                ret.append(f"{label} camera is WarmingUp")
        return ret

    def driver_event_handler(self, event_handle):
        """
        Handles Driver Win32 events from the SDK
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
                            self.logger.info(f"temperature has stabilized at {temp:.2f} degrees")

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
                                self.power_off()
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
                    elif status_code == atmcd_errors.Error_Codes.DRV_IDLE:
                        self.logger.error(f"Driver became IDLE: status_code={error_code(status_code)}")
                    else:
                        self.logger.error(f"Unhandled case: status_code={error_code(status_code)}")
                else:
                    self.logger.error(f"Could not GetStatus() (code={error_code(ret_code)})")

                win32event.ResetEvent(event_handle)
                # self.sdk.SetDriverEvent(0)
            else:
                self.logger.error(f"failed to win32event.WaitForSingleObject() ({result=}")

    # def tec_event_handler(self, event_handle):
    #     """
    #     Handles TEC Win32 events from the SDK
    #     :param event_handle:
    #     :return:
    #     """
    #     while not self._terminated:
    #         result = win32event.WaitForSingleObject(event_handle, win32event.INFINITE)
    #         if result == win32event.WAIT_OBJECT_0:
    #
    #             # when an event arrives, we get the status and temperature status and act accordingly
    #             (ret_code, status_code) = self.sdk.GetTECStatus()
    #             if ret_code == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #                 if status_code == 1:
    #                     self.logger.error(f"TEC event: OVERHEAT")
    #                 elif status_code == 0:
    #                     self.logger.info(f"TEC event: normal")
    #             else:
    #                 self.logger.error(f"Could not GetTECStatus() (code={error_code(ret_code)})")
    #
    #             win32event.ResetEvent(event_handle)
    #             # self.sdk.SetTECEvent(0)
    #         else:
    #             self.logger.error(f"failed to win32event.WaitForSingleObject() ({result=}")

    def set_modes(self,
                  exposure_duration: float | None = None,
                  acquisition_mode: AcquisitionMode | None = None,
                  read_mode: ReadMode | None = None,
                  set_point: float | None = None,
                  cooler_mode: CoolerMode | None = None,
                  activate_cooler: bool | None = None,
                  em_gain: int | None = None,
                  horizontal_binning: int | None = None,
                  vertical_binning: int | None = None,
                  save: bool = False):

        conf = self.conf
        self.exposure_duration = exposure_duration if exposure_duration is not None else conf['exposure_duration'] \
            if 'exposure_duration' in conf else defaults['exposure_duration']

        self.acquisition_mode = acquisition_mode if acquisition_mode is not None else conf['acquisition-mode'] if\
            'acquisition-mode' in conf else defaults['acquisition-mode']

        self.read_mode = read_mode if read_mode is not None else conf['read-mode'] if 'read-mode' in conf \
            else defaults['read-mode']

        self._set_point = set_point if set_point is not None else conf['set-point'] \
            if 'set-point' in conf else defaults['set-point']

        self.cooler_mode = cooler_mode if cooler_mode is not None else conf['cooler-mode'] if 'cooler-mode' in conf \
            else defaults['cooler-mode'].value[0]

        self.em_gain = em_gain if em_gain is not None else conf['em_gain'] if 'em_gain' in conf else defaults['em_gain']

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

        ret = self.sdk.SetAcquisitionMode(self.acquisition_mode.value)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set acquisition mode to {atmcd_capabilities.acquistionModes(self.acquisition_mode.value)}")
        else:
            self.logger.error(f"could not set acquisition mode to SINGLE_SCAN (code={error_code(ret)})")

        ret = self.sdk.SetCoolerMode(self.cooler_mode)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set cooler mode to {self.cooler_mode}")
        else:
            self.logger.error(f"could not set cooler mode to {self.cooler_mode} (code={error_code(ret)})")

        ret = self.sdk.SetReadMode(codes.Read_Mode.IMAGE)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set read mode to {codes.Read_Mode.IMAGE}")
        else:
            self.logger.error(f"could not set acquisition mode to {codes.Read_Mode.IMAGE} (code={error_code(ret)})")

        ret = self.sdk.SetTriggerMode(codes.Trigger_Mode.INTERNAL)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set trigger mode to {codes.Read_Mode.IMAGE}")
        else:
            self.logger.error(f"could not set trigger mode to {codes.Trigger_Mode.INTERNAL} (code={error_code(ret)})")

        ret = self.sdk.SetImage(self.horizontal_binning, self.vertical_binning,
                                1, self.x_pixels, 1, self.y_pixels)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set image to ({self.horizontal_binning=}, {self.vertical_binning=}, " +
                             f"1, {self.x_pixels=}, 1, {self.y_pixels=})")
        else:
            self.logger.error(f"could not set image (code={error_code(ret)})")

        if self.lowest_em_gain > self.em_gain >= self.highest_em_gain:
            raise ValueError(f"bad {self.em_gain=}, must be between {self.lowest_em_gain=} and {self.highest_em_gain=}")

        if 0 <= self.em_gain <= 255:
            ret = self.sdk.SetEMGainMode(0)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.logger.info(f"set EMGainMode to 0")
            else:
                self.logger.error(f"could not set EMGainMode to 0, (code={error_code(ret)})")
        elif 256 <= self.em_gain <= 4095:
            ret = self.sdk.SetEMAdvanced(1)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                ret = self.sdk.SetEMGainMode(1)
                if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                    self.logger.info(f"set EMGainMode to 1")
                else:
                    self.logger.error(f"could not set EMGainMode to 1 (code={error_code(ret)})")
            else:
                self.logger.error(f"could not set EMAdvanced to 1 (code={error_code(ret)})")
        else:
            raise Exception(f"Cannot set em_gain to {self.em_gain} (allowed: 0 >= em_gain <= 4095)")

        ret = self.sdk.SetEMCCDGain(self.em_gain)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set em_gain to {self.em_gain}")
        else:
            self.logger.error(f"could not set em_gain to {self.em_gain} (code={error_code(ret)})")

        ret = self.sdk.SetTemperature(self._set_point)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set set-point to {self._set_point:.2f}")
        else:
            self.logger.error(f"could not set set-point to {self._set_point:.2f}")

        if self.exposure_duration > self.max_exposure_time:
            raise ValueError(f"exposure_duration is over {self.max_exposure_time=}")

        ret = self.sdk.SetExposureTime(self.exposure_duration)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"set exposure_duration to {self.exposure_duration}")
        else:
            self.logger.error(f"could not set exposure_duration to {self.exposure_duration} (code={error_code(ret)})")

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
            self.logger.error(f"could not turn the Cooler {'ON' if on_off else 'OFF'} (code={error_code(ret)})")

    @property
    def is_working(self) -> bool:
        return (self.is_active(NewtonActivities.Acquiring) or
                self.is_active(NewtonActivities.Exposing) or
                self.is_active(NewtonActivities.ReadingOut) or
                self.is_active(NewtonActivities.Saving))

    def _apply_setting(self, func: Callable, arg):
        op = f"sdk.{func.__name__ if hasattr(func, '__name__') else str(func)}({arg})"
        ret = func(*arg) if isinstance(arg, (tuple, list)) else func(arg)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"OK - {op}")
        else:
            code = atmcd_errors.Error_Codes(ret)
            self.append_error(f"FAILED - {op} (error code: {code.name} ({code.value}))")
        return ret

    def apply_settings(self, settings: NewtonCameraSettingsModel):
        self.start_activity(NewtonActivities.SettingParameters)
        self._apply_setting(self.sdk.SetExposureTime, settings.exposure_duration)

        if settings.roi.hend == -1:
            settings.roi.hend = self.x_pixels
        if settings.roi.vend == -1:
            settings.roi.vend = self.y_pixels
        self._apply_setting(self.sdk.SetImage, (settings.binning.x, settings.binning.y,
                                                settings.roi.hstart, settings.roi.hend, settings.roi.vstart, settings.roi.vend))

        self.set_gain(settings)

        self._apply_setting(self.sdk.SetAcquisitionMode, settings.acquisition_mode)
        self._apply_setting(self.sdk.SetShutter, (0, 0, settings.shutter.closing_time, settings.shutter.opening_time))

        self._apply_setting(self.sdk.SetTemperature, settings.temperature.set_point)
        self._apply_setting(self.sdk.SetCoolerMode, settings.temperature.cooler_mode)

        self.latest_camera_settings = settings

        self.end_activity(NewtonActivities.SettingParameters)

    def set_gain(self, settings: NewtonCameraSettingsModel):
        if 0 <= settings.em_gain <= 255:
            self._apply_setting(self.sdk.SetEMGainMode, 0)
        elif 256 <= settings.em_gain <= 4095:
            ret = self._apply_setting(self.sdk.SetEMAdvanced, 1)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self._apply_setting(self.sdk.SetEMGainMode, 1)

        self._apply_setting(self.sdk.SetEMCCDGain, settings.em_gain)

        if 0 <= settings.pre_amp_gain >= self.n_pre_amp_gains:
            self.logger.error(f"bad {settings.pre_amp_gain=}, allowed range(0, {self.n_pre_amp_gains=})")
        else:
            self._apply_setting(self.sdk.SetPreAmpGain, settings.pre_amp_gain)


    def start_acquisition(self, settings: SpecExposureSettings):
        self.acquire(settings=settings)

    def acquire(self, settings: SpecExposureSettings):
        """
        Starts an exposure.
        :param settings: exposure settings
        :return:
        """
        if not self.detected:
            self.logger.error(f"camera not detected")
            return

        if not self._initialized:
            self.logger.error(f"not initialized")
            return

        self.latest_exposure_settings = settings

        self.start_activity(NewtonActivities.Acquiring)
        ret = self.sdk.StartAcquisition()
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.error(f"could not StartAcquisition() (code={error_code(ret)})")
            return
        self.logger.info(f"started exposure with sdk.StartAcquisition()")

        self.start_activity(NewtonActivities.Exposing)

    def readout(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return

        os.makedirs(os.path.dirname(self.latest_exposure_settings.image_file), exist_ok=True)
        self.start_activity(NewtonActivities.ReadingOut)
        ret = self.sdk.SaveAsFITS(self.latest_exposure_settings.image_file, typ=0)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.logger.info(f"saved {self.latest_exposure_settings.image_file}")
        else:
            self.logger.error(f"failed sdk.SaveAsFITS({self.latest_exposure_settings.image_file}, typ=0) (ret={ret}")
        self.end_activity(NewtonActivities.ReadingOut)
        self.end_activity(NewtonActivities.Acquiring)

    def startup(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        self.start_activity(NewtonActivities.StartingUp)
        self.start_activity(NewtonActivities.CoolingDown)
        self.turn_cooler(True)
        self._was_shut_down = False
    
    def shutdown(self):
        if not self.detected:
            self.logger.error(f"camera not detected")
            return
        self.start_activity(NewtonActivities.ShuttingDown)
        self.start_warmup()
        self._was_shut_down = True
    
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
            ret['activities_verbal'] = 'Idle' if self.activities == 0 else self.activities.__repr__()
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
        'em_gain': camera.em_gain,
        'exposure_duration': camera.exposure_duration,

        'activate_cooler': camera.activate_cooler,
        'cooler_mode': camera.cooler_mode,

        'power': {
            'switch': camera.power_switch.ipaddr,
            'outlet': camera.outlet_name,
            'state':  'ON' if camera.is_on() else 'OFF',
        },
    }


def camera_modes() -> dict:
    return {
        'exposure_duration': camera.exposure_duration,
        'acquisition_mode': camera.acquisition_mode,
        'read_mode': camera.read_mode,
        'horizontal_binning': camera.horizontal_binning,
        'vertical_binning': camera.vertical_binning,
        'em_gain': camera.em_gain,
        'set_point': camera.set_point,
        'save': False,
    }


def set_camera_modes(
        exposure_duration: float = Query(description="Exposure length (seconds)", default=defaults['exposure_duration']),
        acquisition_mode: acquisition_modes = Query(description='Select a pre-defined acquisition modes',
                                                    default=defaults['acquisition-mode'].name),
        read_mode: read_modes = Query(description='Select a pre-defined read mode',
                                      default=defaults['read-mode'].name),
        set_point: float = Query(default=defaults['set-point'], description='Target temperature'),
        em_gain: int = Query(default=defaults['em_gain'], ge=1, le=4095),
        horizontal_binning: int = Query(default=defaults['horizontal-binning'], ge=1, le=1600),
        vertical_binning: int = Query(default=defaults['vertical-binning'], ge=1, le=400),
        activate_cooler: bool = Query(default=defaults['activate-cooler']),
        cooler_mode: cooler_modes = Query(default=defaults['cooler-mode'].name,
                                          description='What to do about temperature at shutdown?'),
        save: bool = Query(description='Save these settings as defaults?', default=False),
):
    camera.set_modes(exposure_duration=exposure, acquisition_mode=getattr(AcquisitionMode, acquisition_mode.value),
                     read_mode=getattr(ReadMode, read_mode.value), set_point=set_point,
                     cooler_mode=getattr(CoolerMode, cooler_mode.value), activate_cooler=activate_cooler,
                     em_gain=em_gain, horizontal_binning=horizontal_binning, vertical_binning=vertical_binning,
                     save=save)


base_path = BASE_SPEC_PATH + 'highspec/camera'
tag = 'HighSpec Camera'
router = APIRouter()

camera = NewtonEMCCD()

router.add_api_route(base_path, tags=[tag], endpoint=show_camera)
# router.add_api_route(base_path + '/expose', tags=[tag], endpoint=camera.expose)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=camera.status)
router.add_api_route(base_path + '/set-modes', tags=[tag], endpoint=set_camera_modes)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=camera.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=camera.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=camera.abort)




if __name__ == '__main__':

    camera.startup()
    while camera.is_active(NewtonActivities.StartingUp):
        camera.logger.debug(f"waiting for NewtonActivities.StartingUp to end ...")
        time.sleep(5)

    camera.acquire(SpecExposureSettings(exposure_duration=5, number_of_exposures=1, output_folder='c:/tmp'))
    while camera.is_active(NewtonActivities.Exposing):
        camera.logger.debug(f"waiting for NewtonActivities.Exposing to end ...")
        time.sleep(5)

    print("done")
