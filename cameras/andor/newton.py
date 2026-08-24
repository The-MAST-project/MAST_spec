import threading
import time
from collections.abc import Callable
from enum import Enum, StrEnum
from pathlib import Path
from typing import Annotated, Literal, cast

import win32event
from astropy.io import fits
from fastapi import Query
from pyAndorSDK2 import (
    CameraCapabilities,
    atmcd,
    atmcd_capabilities,
    atmcd_codes,
    atmcd_errors,
)
from pyAndorSDK2.atmcd import AndorCapabilities

from common.activities import NewtonActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.shutter import ShutterConfig
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.newton import (
    NewtonAmplifierMode,
    NewtonBinning,
    NewtonHSSpeed,
    NewtonRoi,
    NewtonSettingsConfig,
    NewtonTemperatureConfig,
)
from common.models.statuses import NewtonStatus
from common.paths import PathMaker
from common.spec import (
    CLOSED_SHUTTER_FRAMES,
    FrameType,
    SpecExposureSettings,
    integration_duration_for,
)
from common.utils import function_name

logger = get_logger(__name__)
label = "EMCCD"


class AcquisitionMode(Enum):
    SINGLE_SCAN = 1
    ACCUMULATE = 2
    KINETICS = 3
    FAST_KINETICS = 4
    RUN_TILL_ABORT = 5


acquisition_modes = Enum("AcquisitionModes", {name: name for name in AcquisitionMode.__members__})


class ReadMode(Enum):
    FULL_VERTICAL_BINNING = 0
    MULTI_TRACK = 1
    RANDOM_TRACK = 2
    SINGLE_TRACK = 3
    IMAGE = 4


read_modes = Enum("ReadModes", {name: name for name in ReadMode.__members__})


class CoolerMode(Enum):
    RETURN_TO_AMBIENT = 0
    MAINTAIN_CURRENT_TEMP = 1


cooler_modes = Enum("CoolerModes", {name: name for name in CoolerMode.__members__})


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


class NewtonEMGainRange:
    low: int
    high: int


# SetShutter mode, per the SDK: 0 automatic, 1 permanently open, 2 permanently closed.
_SHUTTER_AUTOMATIC = 0
_SHUTTER_CLOSED = 2


def _shutter_mode_for(frame_type: FrameType) -> int:
    """The SetShutter mode a frame of this type needs.

    Automatic for light and flat frames -- the camera opens the shutter for the integration
    and closes it for the readout. Closed for bias and dark, which are defined by the sensor
    seeing nothing.
    """
    return _SHUTTER_CLOSED if frame_type in CLOSED_SHUTTER_FRAMES else _SHUTTER_AUTOMATIC


class NewtonPreAmpGain(StrEnum):
    x1 = "1x"
    x2 = "2x"
    x4 = "4x"


_pre_amp_gains = {
    NewtonPreAmpGain.x1: 0,
    NewtonPreAmpGain.x2: 1,
    NewtonPreAmpGain.x4: 2,
}

# The config stores the SDK's index, the endpoint takes the name. Derived, so the two
# spellings cannot drift apart.
_pre_amp_gain_by_index = {index: gain for gain, index in _pre_amp_gains.items()}


# NewtonHSSpeed comes from MAST_common, like NewtonAmplifierMode: the config model, the
# database and this endpoint have to agree on the spelling, and a second definition here
# would be a copy to keep in step. This map is the part that is ours -- the SDK's speed
# indices, in the order the camera reports them.
_horizontal_shift_speed_index = {
    NewtonHSSpeed.MHz_3_0: 0,
    NewtonHSSpeed.MHz_1_0: 1,
    NewtonHSSpeed.MHz_0_05: 2,
}


def error_code(code) -> str:
    return atmcd_errors.Error_Codes(code).__repr__()


class NewtonEMCCD(Component, SwitchedOutlet):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        from common.config import Config

        self.logger = get_logger("mast.spec.highspec.camera")
        self.log_label = "EMCCD"

        Component.__init__(self, NewtonActivities)
        self._name = "highspec"

        # NOTE: The power to this camera is switched on by spec.startup()
        SwitchedOutlet.__init__(self, outlet_name="Highspec", domain=OutletDomain.SpecOutlets)

        self._detected = False
        self.conf: NewtonSettingsConfig = cast(NewtonSettingsConfig, Config().get_specs().highspec.settings)

        self.SensorTemp = float("nan")
        self.TargetTemp = float("nan")
        self.AmbientTemp = float("nan")
        self.CoolerVolts = float("nan")

        self._set_point: int | None = None
        self.acquisition_mode: AcquisitionMode | None = None
        self.read_mode: ReadMode | None = None
        self.cooler_mode: CoolerMode | int | None = None
        self.em_gain: int | None = None
        self.em_gain_range: NewtonEMGainRange | None = None
        self.horizontal_binning: int | None = None
        self.vertical_binning: int | None = None
        self.activate_cooler: bool | None = None
        self.exposure_duration: float | None = None
        self.errors: list[str] = []
        self.latest_exposure_settings: SpecExposureSettings | None = None
        # What the camera said it would actually expose for, from GetAcquisitionTimings.
        # None until an exposure has been configured, or if that call failed.
        self.actual_exposure_duration: float | None = None

        assert self.power_switch is not None
        if self.power_switch.detected and not self.is_on():
            self.power_on()

        self.enabled = self.conf.camera_enabled
        if not self.enabled:
            self.info("Camera is disabled.")
            self._initialized = True
            return

        self._initialized = False

        if not self.power_switch.detected:
            self.warning(f"power switch {self.power_switch} not detected")

        self.sdk = atmcd()
        ret = self.sdk.Initialize("")
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.error(f"Could not initialize ANDOR SDK (code={error_code(ret)})")
            return

        # self.parse_camera_capabilities()

        (ret, serial_number) = self.sdk.GetCameraSerialNumber()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.serial_number = serial_number
            self._detected = True
        else:
            self.error(f"Could not get serial number (code={error_code(ret)})")
            self.sdk.ShutDown()
            return

        (ret, capabilities) = self.sdk.GetCapabilities()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.caps: AndorCapabilities = capabilities  # type: ignore
        else:
            self.error(f"Could not GetCapabilities() (code={error_code(ret)})")

        if not self.caps.ulCameraType & atmcd_capabilities.cameratype.AC_CAMERATYPE_NEWTON:
            raise RuntimeError("the camera is not a NEWTON")

        self.info(f"found a NEWTON camera, SN: {self.serial_number}")
        self.supports_em_advanced = bool(
            self.caps.ulSetFunctions & atmcd_capabilities.SetFunctions.AC_SETFUNCTION_EMADVANCED
        )

        if not self.caps.ulSetFunctions & atmcd_capabilities.SetFunctions.AC_SETFUNCTION_EMCCDGAIN:
            self.warning("no AC_SETFUNCTION_EMCCDGAIN capability")

        (ret, x_pixels, y_pixels) = self.sdk.GetDetector()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.x_pixels = x_pixels
            self.y_pixels = y_pixels
        else:
            self.error(f"Could not GetDetector() (code={error_code(ret)})")
            self.sdk.ShutDown()
            return
        self.info(f"detector size: {self.x_pixels}x{self.y_pixels}")

        self.min_temp: float | None = None
        self.max_temp: float | None = None
        (ret, min_temp, max_temp) = self.sdk.GetTemperatureRange()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.min_temp = min_temp
            self.max_temp = max_temp
            self.info(f"got temperature range: {self.min_temp}, {self.max_temp}")
        else:
            self.error(f"could not GetTemperatureRange() (code={error_code(ret)})")

        # Max exposure time
        (ret, max_exposure_time) = self.sdk.GetMaximumExposure()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.max_exposure_time = max_exposure_time
            self.info(f"got max exposure_duration time: {self.max_exposure_time}")
        else:
            self.error(f"could not GetMaximumExposure() (code={error_code(ret)})")

        # Amplifier modes
        self._apply_setting(
            self.sdk.SetOutputAmplifier,
            0 if self.conf.amplifier_mode == "conventional" else 1,
        )

        # EM Gain range
        (ret, low, high) = self.sdk.GetEMGainRange()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.lowest_em_gain = low
            self.highest_em_gain = high
            self.info(f"got em gain range: {self.lowest_em_gain}, {self.highest_em_gain}")
        else:
            self.error(f"could not GetEMGainRange() ({ret=})")

        # GetNumber Horizontal Shift Speeds
        amplifier_mode_numeric = 0
        self.hs_speeds = dict[NewtonAmplifierMode, list[float]]()
        (ret, n_hs_speeds) = self.sdk.GetNumberHSSpeeds(channel=0, typ=amplifier_mode_numeric)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            for i in range(n_hs_speeds):
                ret, speed = self.sdk.GetHSSpeed(channel=0, typ=amplifier_mode_numeric, index=i)
                if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                    self.hs_speeds.setdefault("em", []).append(speed)
                else:
                    self.error(f"could not GetHSSpeed() for channel 0, 'em' mode (index={i}, code={error_code(ret)})")
            self.info(f"got horizontal shift speeds for 'em' mode: {self.hs_speeds.get('em', [])}")
        else:
            self.error(f"could not GetNumberHSSpeeds(channel=0, typ='em'') ({ret=})")

        amplifier_mode_numeric = 1
        (ret, n_hs_speeds) = self.sdk.GetNumberHSSpeeds(channel=0, typ=amplifier_mode_numeric)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            for i in range(n_hs_speeds):
                ret, speed = self.sdk.GetHSSpeed(channel=0, typ=amplifier_mode_numeric, index=i)
                if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                    self.hs_speeds.setdefault("conventional", []).append(speed)
                else:
                    self.error(
                        f"could not GetHSSpeed() for channel 0, 'conventional' mode (index={i}, code={error_code(ret)})"
                    )
            self.info(f"got horizontal shift speeds for 'conventional' mode: {self.hs_speeds.get('conventional', [])}")
        else:
            self.error(f"could not GetNumberHSSpeeds(channel=0, typ='conventional') ({ret=})")

        ret, n_ad_channels = self.sdk.GetNumberADChannels()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.info(f"got number of AD channels: {n_ad_channels}")
        else:
            self.error(f"could not GetNumberADChannels() ({ret=})")
        if n_ad_channels == 1:
            self._apply_setting(self.sdk.SetADChannel, 0)
        else:
            self.warning(f"camera has {n_ad_channels} AD channels, but only channel 0 is supported by this software")

        # TODO: check if our camera can generate ESD events

        # Cooling is the only camera setting still applied here, because it is not a
        # per-exposure choice: it is a physical process with its own activities
        # (CoolingDown / WarmingUp) and its own endpoints, and re-issuing it before each
        # exposure would fight them. Everything else -- read mode, acquisition mode,
        # geometry, amplifier, gains, shutter, exposure time -- is applied per exposure by
        # _apply_exposure_settings, so no exposure depends on what a previous one left.
        if self.conf.temperature is not None:
            self.set_point = self.conf.temperature.regular_set_point
            self._apply_setting(self.sdk.SetTemperature, self.conf.temperature.regular_set_point)
            self._apply_setting(self.sdk.SetCoolerMode, self.conf.temperature.cooler_mode)

        driver_event_handle = win32event.CreateEvent(None, 0, 0, None)
        ret = self.sdk.SetDriverEvent(int(driver_event_handle))
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.info("set driver event handler")
            self._terminated = False
            self.driver_event_handler_thread = threading.Thread(
                name="event-handler-thread",
                target=self.driver_event_handler,
                args=(driver_event_handle,),
            )
            self.driver_event_handler_thread.start()
        else:
            self.error(f"Could not set driver event handler (code={error_code(ret)})")

        # self.start_cooldown()
        self._was_shut_down = False
        self.parent_spec = None

        self._initialized = True

    def set_parent_spec(self, parent):
        self.parent_spec = parent

    def append_error(self, err: str):
        self.errors.append(err)
        self.error(err)

    def parse_camera_capabilities(self):
        """
        Parse and print capabilities returned by sdk GetCapabilities()
        :return:
        """
        helper = CameraCapabilities.CapabilityHelper(self.sdk)
        print("capabilities")
        helper.print_all()

    def start_cooldown(self, target_set_point: Literal["regular", "science"]):
        match target_set_point:
            case "science":
                set_point = self.conf.temperature.science_set_point if self.conf.temperature else None
            case "regular":
                set_point = self.conf.temperature.regular_set_point if self.conf.temperature else None

        self.turn_cooler(True)
        ret = self.sdk.SetTemperature(set_point)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.error(f"failed to set temperature to {set_point} degrees (code={error_code(ret)})")
            return
        self.start_activity(NewtonActivities.CoolingDown, details=[f"set_point={set_point}"])

    def start_warmup(self):
        self.turn_cooler(True)
        target_temp = self.max_temp
        ret = self.sdk.SetTemperature(target_temp)
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.error(f"failed to set temperature to {target_temp} degrees (code={error_code(ret)})")
            return
        self.start_activity(NewtonActivities.WarmingUp, details=[f"set_point={target_temp}"])

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
        return (
            self.enabled
            and (self.power_switch.detected if self.power_switch is not None else False)
            and self.is_on()
            and self.detected
            # and not (
            #     self.is_active(NewtonActivities.CoolingDown)
            #     or self.is_active(NewtonActivities.WarmingUp)
            # )
        )

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        label = "highspec:"
        if not self.enabled:
            ret.append(f"{label} camera is disabled by configuration")
        else:
            if not self.power_switch or not self.power_switch.detected:
                ret.append(f"{label} {self.power_switch} not detected")
            elif self.is_off():
                ret.append(f"{label} {self.power_switch}:{self.outlet_names[0]} is OFF")
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
                        threading.Thread(name="highspec-readout", target=self.readout).start()

                    elif self.is_active(NewtonActivities.CoolingDown) or self.is_active(NewtonActivities.WarmingUp):
                        (temp_code, temp) = self.sdk.GetTemperatureF()
                        if temp_code == atmcd_errors.Error_Codes.DRV_TEMPERATURE_STABILIZED:
                            self.info(f"temperature has stabilized at {temp:.2f} degrees")

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
                                        self.error(f"could not turn cooler OFF (code={error_code(ret)})")
                                    power_off = True
                            if power_off:
                                self.power_off()
                        else:
                            self.error(f"Could not GetTemperatureF() (code={error_code(temp_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ERROR_ACK:
                        self.error(f"Driver cannot communicate with the camera (code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_BUFFER:
                        self.error(f"Driver cannot read data at required rate (code={error_code(status_code)})")

                    elif status_code == atmcd_errors.Error_Codes.DRV_ACQ_DOWNFIFO_FULL:
                        self.error(
                            f"Driver cannot read data fast enough to prevent FIFO overflow (code={error_code(status_code)})"
                        )
                    elif status_code == atmcd_errors.Error_Codes.DRV_IDLE:
                        self.error(f"Driver became IDLE: status_code={error_code(status_code)}")
                    else:
                        self.error(f"Unhandled case: status_code={error_code(status_code)}")
                else:
                    self.error(f"Could not GetStatus() (code={error_code(ret_code)})")

                win32event.ResetEvent(event_handle)
                # self.sdk.SetDriverEvent(0)
            else:
                self.error(f"failed to win32event.WaitForSingleObject() ({result=}")

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

    @staticmethod
    def defaults(_) -> NewtonSettingsConfig:  # type: ignore
        from common.config.shutter import ShutterConfig

        return NewtonSettingsConfig(
            exposure_duration=10,
            acquisition_mode=AcquisitionMode.SINGLE_SCAN.value,
            number_of_exposures=1,
            em_gain=200,
            binning=NewtonBinning(x=1, y=1),
            shutter=ShutterConfig(open_time=20, close_time=12, automatic=True),
            temperature=NewtonTemperatureConfig(regular_set_point=-10, science_set_point=-85),
        )

    # def set_modes(
    #     self,
    #     exposure_duration: float | None = None,
    #     acquisition_mode: AcquisitionMode | None = None,
    #     read_mode: ReadMode | None = None,
    #     set_point: int | None = None,
    #     cooler_mode: CoolerMode | None = None,
    #     activate_cooler: bool | None = None,
    #     em_gain: int | None = None,
    #     horizontal_binning: int | None = None,
    #     vertical_binning: int | None = None,
    #     save: bool = False,
    # ):
    #     conf = self.conf
    #     self.exposure_duration = (
    #         exposure_duration
    #         if exposure_duration is not None
    #         else conf.settings.exposure_duration
    #     )

    #     self.acquisition_mode = (
    #         acquisition_mode
    #         if acquisition_mode is not None
    #         else AcquisitionMode(conf.settings.acquisition_mode)
    #     )

    #     self.read_mode = (
    #         read_mode if read_mode is not None else ReadMode(conf.settings.read_mode)
    #     )

    #     assert conf.settings.temperature
    #     assert NewtonEMCCD.defaults.temperature
    #     self._set_point = (
    #         set_point
    #         if set_point is not None
    #         else conf.settings.temperature.set_point
    #         if conf.settings.temperature.set_point is not None
    #         else NewtonEMCCD.defaults.temperature.set_point
    #     )

    #     self.cooler_mode = (
    #         cooler_mode
    #         if cooler_mode is not None
    #         else conf.settings.temperature.cooler_mode
    #     )

    #     self.em_gain = (
    #         em_gain
    #         if em_gain is not None
    #         else conf.settings.em_gain
    #         if conf.settings.em_gain is not None
    #         else NewtonEMCCD.defaults.em_gain
    #     )

    #     binning = (
    #         conf.settings.binning
    #         if conf.settings.binning is not None
    #         else NewtonEMCCD.defaults.binning
    #         if NewtonEMCCD.defaults.binning
    #         else BinningModel(x=1, y=1)
    #     )
    #     self.horizontal_binning = (
    #         horizontal_binning if horizontal_binning is not None else binning.x
    #     )

    #     self.vertical_binning = (
    #         vertical_binning if vertical_binning is not None else binning.y
    #     )

    #     assert self.horizontal_binning
    #     (ret, max_horizontal_binning) = self.sdk.GetMaximumBinning(
    #         self.read_mode.value, 0
    #     )
    #     if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.error(
    #             f"could not sdk.GetMaximumBinning({self.read_mode.value}, 0) (ret={ret}"
    #         )
    #     elif self.horizontal_binning > max_horizontal_binning:
    #         return {
    #             "error": f"Horizontal binning for ReadMode {self.read_mode.name} cannot exceed {max_horizontal_binning}"
    #         }

    #     assert self.vertical_binning
    #     (ret, max_vertical_binning) = self.sdk.GetMaximumBinning(
    #         self.read_mode.value, 1
    #     )
    #     if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.error(
    #             f"could not sdk.GetMaximumBinning({self.read_mode.value}, 1) (ret={ret}"
    #         )
    #     elif self.vertical_binning > max_vertical_binning:
    #         return {
    #             "error": f"Vertical binning for ReadMode {self.read_mode.name} cannot exceed {max_vertical_binning}"
    #         }

    #     ret = self.sdk.SetAcquisitionMode(self.acquisition_mode)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(
    #             f"set acquisition mode to {atmcd_capabilities.acquistionModes(self.acquisition_mode)}"
    #         )
    #     else:
    #         self.logger.error(
    #             f"could not set acquisition mode to SINGLE_SCAN (code={error_code(ret)})"
    #         )

    #     ret = self.sdk.SetCoolerMode(self.cooler_mode)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set cooler mode to {self.cooler_mode}")
    #     else:
    #         self.logger.error(
    #             f"could not set cooler mode to {self.cooler_mode} (code={error_code(ret)})"
    #         )

    #     ret = self.sdk.SetReadMode(codes.Read_Mode.IMAGE)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set read mode to {codes.Read_Mode.IMAGE}")
    #     else:
    #         self.logger.error(
    #             f"could not set acquisition mode to {codes.Read_Mode.IMAGE} (code={error_code(ret)})"
    #         )

    #     ret = self.sdk.SetTriggerMode(codes.Trigger_Mode.INTERNAL)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set trigger mode to {codes.Read_Mode.IMAGE}")
    #     else:
    #         self.logger.error(
    #             f"could not set trigger mode to {codes.Trigger_Mode.INTERNAL} (code={error_code(ret)})"
    #         )

    #     ret = self.sdk.SetImage(
    #         self.horizontal_binning,
    #         self.vertical_binning,
    #         1,
    #         self.x_pixels,
    #         1,
    #         self.y_pixels,
    #     )
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(
    #             f"set image to ({self.horizontal_binning=}, {self.vertical_binning=}, "
    #             + f"1, {self.x_pixels=}, 1, {self.y_pixels=})"
    #         )
    #     else:
    #         self.logger.error(f"could not set image (code={error_code(ret)})")

    #     if self.lowest_em_gain > self.em_gain >= self.highest_em_gain:
    #         raise ValueError(
    #             f"bad {self.em_gain=}, must be between {self.lowest_em_gain=} and {self.highest_em_gain=}"
    #         )

    #     if 0 <= self.em_gain <= 255:
    #         ret = self.sdk.SetEMGainMode(0)
    #         if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #             self.logger.info("set EMGainMode to 0")
    #         else:
    #             self.logger.error(
    #                 f"could not set EMGainMode to 0, (code={error_code(ret)})"
    #             )
    #     elif 256 <= self.em_gain <= 4095:
    #         ret = self.sdk.SetEMAdvanced(1)
    #         if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #             ret = self.sdk.SetEMGainMode(1)
    #             if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #                 self.logger.info("set EMGainMode to 1")
    #             else:
    #                 self.logger.error(
    #                     f"could not set EMGainMode to 1 (code={error_code(ret)})"
    #                 )
    #         else:
    #             self.logger.error(
    #                 f"could not set EMAdvanced to 1 (code={error_code(ret)})"
    #             )
    #     else:
    #         raise Exception(
    #             f"Cannot set em_gain to {self.em_gain} (allowed: 0 >= em_gain <= 4095)"
    #         )

    #     ret = self.sdk.SetEMCCDGain(self.em_gain)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set em_gain to {self.em_gain}")
    #     else:
    #         self.logger.error(
    #             f"could not set em_gain to {self.em_gain} (code={error_code(ret)})"
    #         )

    #     ret = self.sdk.SetTemperature(self._set_point)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set set-point to {self._set_point:.2f}")
    #     else:
    #         self.logger.error(f"could not set set-point to {self._set_point:.2f}")

    #     if self.exposure_duration > self.max_exposure_time:
    #         raise ValueError(f"exposure_duration is over {self.max_exposure_time=}")

    #     ret = self.sdk.SetExposureTime(self.exposure_duration)
    #     if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #         self.logger.info(f"set exposure_duration to {self.exposure_duration}")
    #     else:
    #         self.logger.error(
    #             f"could not set exposure_duration to {self.exposure_duration} (code={error_code(ret)})"
    #         )

    #     if self.activate_cooler:
    #         self.start_activity(NewtonActivities.CoolingDown)
    #         self.turn_cooler(True)
    #     else:
    #         self.turn_cooler(False)

    #     if save:
    #         # TODO: update conf and toml.save it
    #         self.logger.error("save is not implemented yet!")

    @property
    def set_point(self):
        return self._set_point

    @set_point.setter
    def set_point(self, value: int):
        self._set_point = value

    def turn_cooler(self, on_off: bool):
        if not self.detected:
            self.error("camera not detected")
            return

        ret = self.sdk.CoolerON() if on_off else self.sdk.CoolerOFF()
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.info(f"turned the cooler {'ON' if on_off else 'OFF'}")
        else:
            self.error(f"could not turn the Cooler {'ON' if on_off else 'OFF'} (code={error_code(ret)})")

    @property
    def is_working(self) -> bool:
        return (
            self.is_active(NewtonActivities.Acquiring)
            or self.is_active(NewtonActivities.Exposing)
            or self.is_active(NewtonActivities.ReadingOut)
            or self.is_active(NewtonActivities.Saving)
        )

    def _apply_setting(self, func: Callable, arg):
        op = f"sdk.{func.__name__ if hasattr(func, '__name__') else str(func)}({arg})"
        ret = func(*arg) if isinstance(arg, (tuple, list)) else func(arg)
        if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.info(f"OK - {op}")
        else:
            code = atmcd_errors.Error_Codes(ret)
            self.append_error(f"{self.log_label}: FAILED - {op} (error code: {code.name} ({code.value}))")
        return ret

    # def set_gain(self, settings: NewtonSettingsConfig):
    #     if settings.em_gain is not None:
    #         if 0 <= settings.em_gain <= 255:
    #             self._apply_setting(self.sdk.SetEMGainMode, 0)
    #         elif 256 <= settings.em_gain <= 4095:
    #             ret = self._apply_setting(self.sdk.SetEMAdvanced, 1)
    #             if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
    #                 self._apply_setting(self.sdk.SetEMGainMode, 1)

    #         self._apply_setting(self.sdk.SetEMCCDGain, settings.em_gain)

    #     if settings.pre_amp_gain is not None:
    #         if 0 <= settings.pre_amp_gain >= self.n_pre_amp_gains:
    #             self.error(
    #                 f"bad {settings.pre_amp_gain=}, allowed range(0, {self.n_pre_amp_gains=})"
    #             )
    #         else:
    #             self._apply_setting(self.sdk.SetPreAmpGain, settings.pre_amp_gain)

    def start_acquisition(self, settings: SpecExposureSettings):
        self.debug(f"{function_name()} settings: {settings}")
        self.latest_exposure_settings = settings
        self.acquire(settings=settings)

    def _apply_exposure_settings(
        self,
        exposure_duration: float,
        amplifier_mode: NewtonAmplifierMode,
        em_gain: int,
        pre_amp_gain: NewtonPreAmpGain,
        horizontal_shift_speed: NewtonHSSpeed,
        binning: NewtonBinning,
        roi: NewtonRoi | None,
        shutter: ShutterConfig | None,
        acquisition_mode: int,
        frame_type: FrameType = FrameType.LIGHT,
    ) -> CanonicalResponse:
        """Apply exactly these settings to the camera. Reads no configuration of its own.

        Every setting, every exposure. There used to be a settings blob -- apply_settings()
        -- pushed onto the camera once at startup, with exposures relying on that context
        still holding. It did not: expose_single_image reconfigured the amplifier, shift
        speed, gains and shutter for itself, while the plan path set only the exposure time
        and inherited the rest. A plan exposure taken after an autofocus run therefore used
        the autofocus's amplifier and gains, and nothing said so.

        The first version of this method still read acquisition_mode, roi and shutter from
        `self.conf` while taking the other six as arguments -- a split with no principle
        behind it beyond "the ones the endpoint already exposed". It made the docstring
        above false for three settings, put the resolution rule in two places, and meant no
        caller could express those three even in principle. They are arguments now, and
        deciding where a value comes from belongs to the caller: the endpoint resolves
        parameter-or-config, the plan path resolves config.

        Read mode is the one exception, hardcoded to IMAGE. A spectrograph could legitimately
        want full vertical binning or a single track, but that is a science decision nobody
        has made, and the camera has been in IMAGE mode all along -- every frame on the share
        reports READMODE='Image'. Stating it beats inheriting it, and makes the assumption
        greppable the day someone wants FVB.
        """
        self.start_activity(NewtonActivities.SettingParameters)
        try:
            self._apply_setting(self.sdk.SetReadMode, atmcd_codes.Read_Mode.IMAGE)
            self._apply_setting(self.sdk.SetAcquisitionMode, acquisition_mode)

            # No ROI means the full sensor, and it is applied rather than assumed. The old
            # code skipped SetImage entirely when the ROI was absent, which left the geometry
            # AND the binning at whatever the previous exposure had set -- so the binning an
            # assignment asked for never reached the camera unless a region happened to be
            # configured too.
            self._apply_setting(
                self.sdk.SetImage,
                (
                    binning.x,
                    binning.y,
                    roi.hstart if roi is not None else 1,
                    roi.hend if roi is not None else self.x_pixels,
                    roi.vstart if roi is not None else 1,
                    roi.vend if roi is not None else self.y_pixels,
                ),
            )

            response = self._apply_amplifier_settings(
                amplifier_mode=amplifier_mode,
                em_gain=em_gain,
                pre_amp_gain=pre_amp_gain,
                horizontal_shift_speed=horizontal_shift_speed,
            )
            if response.failed:
                return response

            # The frame type decides the shutter, which is the whole point of asking for one.
            # SetShutter's second argument is the mode: 0 automatic, 1 permanently open,
            # 2 permanently closed (SDK, SetShutter). It was hardcoded to 0, so a frame
            # requested as `dark` or `bias` was exposed with the shutter opening exactly as a
            # light frame's does -- a light frame with a misleading name, and the name was the
            # only place the request was recorded at all.
            #
            # Applied even when `shutter` is None. The close/open transfer times come from the
            # config and are only needed for the SDK's timing arithmetic, but the MODE is not
            # a timing detail: skipping the call left the camera on whatever the previous
            # exposure set, which is how an unconfigured shutter would silently produce a lit
            # "dark". 0 ms transfer times are what the SDK documents for cameras without a
            # shutter, and are harmless where one is configured but absent from this call.
            closing_time = shutter.close_time if shutter is not None else 0
            opening_time = shutter.open_time if shutter is not None else 0
            self._apply_setting(
                self.sdk.SetShutter,
                (0, _shutter_mode_for(frame_type), closing_time, opening_time),
            )

            # Last, deliberately: the SDK quantises the requested time against the readout
            # configuration above, so it has to be set once that configuration is in place.
            self._apply_setting(self.sdk.SetExposureTime, exposure_duration)
            return self._record_actual_exposure_duration(exposure_duration)
        finally:
            self.end_activity(NewtonActivities.SettingParameters)

    # A difference below this is the camera rounding to its internal clock, not a problem.
    # Above it, something was asked for that the configuration cannot deliver -- a 1 ms
    # exposure with a 20 ms shutter transfer, say.
    EXPOSURE_TOLERANCE_FRACTION = 0.01
    EXPOSURE_TOLERANCE_SECONDS = 0.001

    def _record_actual_exposure_duration(self, requested: float) -> CanonicalResponse:
        """Ask the camera what exposure it will actually use, and remember it.

        GetAcquisitionTimings is the SDK's own answer to this question, and the vendor's
        documentation says to call it once every acquisition setting is in place -- which is
        why it lives at the end of _apply_exposure_settings. Its example is precisely our
        case: "it is possible to set the exposure time to 20ms ... and then set the readout
        mode to full image. As it can take 250ms to read out an image it is not possible to
        have a cycle time of 30ms."

        A difference is normal: the camera quantises the request to its clock, so asking for
        1.0 and being given 0.999983 is the SDK working. The exposure therefore goes ahead
        whatever comes back -- refusing would turn a usable frame into no frame, when the
        camera has just said exactly what it will do. What must not happen is silence: past
        the tolerance this warns, and readout() writes the real value into the FITS header
        rather than the request, which is what the header used to claim.

        The one failure worth refusing is the call itself. DRV_INVALID_MODE means the
        acquisition and readout modes are not a combination the camera has, so the frame
        would be meaningless.
        """
        ret, actual, _accumulate, _kinetic = self.sdk.GetAcquisitionTimings()
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            err = f"could not GetAcquisitionTimings() (code={error_code(ret)})"
            self.error(err)
            self.actual_exposure_duration = None
            return CanonicalResponse(errors=[err])

        self.actual_exposure_duration = actual

        # A zero request is not a duration, it is "give me your floor" -- the SDK takes "the
        # nearest valid value not less than the given value", so being handed something
        # larger is the request being GRANTED. Warning about it would fire on every bias
        # frame, and a warning that always fires is one nobody reads.
        #
        # Reported at info instead, because the number is worth having: the floor depends on
        # the shift speed, ROI and binning in force, so there is no single value to look up
        # and this line is where it can be observed.
        if requested == 0:
            self.info(f"shortest exposure this readout configuration allows: {actual} seconds")
            return CanonicalResponse_Ok

        tolerance = max(requested * self.EXPOSURE_TOLERANCE_FRACTION, self.EXPOSURE_TOLERANCE_SECONDS)
        if abs(actual - requested) > tolerance:
            self.warning(
                f"the camera will expose for {actual} seconds, not the {requested} requested "
                f"(over the {tolerance:g}s tolerance); the readout configuration cannot deliver it"
            )
        else:
            self.info(f"exposure time: requested {requested}s, camera will use {actual}s")
        return CanonicalResponse_Ok

    def _apply_amplifier_settings(
        self,
        amplifier_mode: NewtonAmplifierMode,
        em_gain: int,
        pre_amp_gain: NewtonPreAmpGain,
        horizontal_shift_speed: NewtonHSSpeed,
    ) -> CanonicalResponse:
        """The amplifier, its shift speed and its gains -- one combination, checked first.

        IsPreAmpGainAvailable() is asked before anything is applied, because which pre-amp
        gains are valid depends on both the amplifier and the shift speed. A combination the
        camera rejects is better refused whole than applied in part.
        """
        horizontal_shift_index = _horizontal_shift_speed_index[horizontal_shift_speed]
        pre_amp_gain_index = _pre_amp_gains[pre_amp_gain]
        amplifier_mode_numeric = 0 if amplifier_mode == "em" else 1

        ret, available = self.sdk.IsPreAmpGainAvailable(
            channel=0,
            amplifier=amplifier_mode_numeric,
            index=horizontal_shift_index,
            pa=pre_amp_gain_index,
        )
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            err = (
                f"failed to check whether pre-amp gain {pre_amp_gain} is available for "
                f"{horizontal_shift_speed} in '{amplifier_mode}' mode (code={error_code(ret)})"
            )
            self.error(err)
            return CanonicalResponse(errors=[err])
        if not available:
            err = f"pre-amp gain {pre_amp_gain} is not available for {horizontal_shift_speed} in '{amplifier_mode}' mode"
            self.error(err)
            return CanonicalResponse(errors=[err])

        self._apply_setting(self.sdk.SetOutputAmplifier, amplifier_mode_numeric)
        self._apply_setting(self.sdk.SetHSSpeed, (amplifier_mode_numeric, horizontal_shift_index))
        if amplifier_mode == "em":
            self._apply_setting(self.sdk.SetEMGainMode, 0)
            self._apply_setting(self.sdk.SetEMCCDGain, em_gain)
        self._apply_setting(self.sdk.SetPreAmpGain, pre_amp_gain_index)
        return CanonicalResponse_Ok

    def acquire(self, settings: SpecExposureSettings):
        """Configure the camera from the config, then start the exposure.

        The plan path. A plan deliberately carries no EMCCD detail -- a scientist writing
        one should not have to name an amplifier or a pre-amp gain -- so everything except
        exposure time and binning comes from the config. Callers that resolve their own
        settings (expose_single_image does, from its parameters) apply them first and call
        `start_exposure` instead, or this would overwrite their choices with the config's:
        it did, for a while.
        """
        if not self.detected:
            self.error("camera not detected")
            return

        if not self._initialized:
            self.error("not initialized")
            return

        # The plan path's clamp. A plan carries a frame_type and a duration independently, so
        # it is the one place a "bias" can arrive with five seconds attached to it.
        #
        # A copy rather than an in-place edit: `settings` belongs to the caller, and the same
        # object reaches start_exposure -> latest_exposure_settings -> the FITS header. The
        # copy keeps the clamp in one direction and leaves the plan's own record alone.
        requested_duration = settings.exposure_duration
        clamped_duration = integration_duration_for(settings.frame_type, requested_duration)
        if clamped_duration != requested_duration:
            self.info(
                f"frame type is {settings.frame_type.value}: asking the camera for its shortest "
                f"exposure, not the {requested_duration} seconds the plan asked for"
            )
            settings = settings.model_copy(update={"exposure_duration": clamped_duration})

        response = self._apply_exposure_settings(
            exposure_duration=settings.exposure_duration,
            amplifier_mode=self.conf.amplifier_mode,
            em_gain=self.conf.em_gain,
            pre_amp_gain=_pre_amp_gain_by_index[self.conf.pre_amp_gain],
            horizontal_shift_speed=self.conf.horizontal_shift_speed,
            binning=NewtonBinning(x=settings.x_binning or 1, y=settings.y_binning or 1),
            roi=self.conf.roi,
            shutter=self.conf.shutter,
            acquisition_mode=self.conf.acquisition_mode,
            # From the plan, not the config: SpecExposureSettings has carried a frame_type all
            # along and this path dropped it, so a plan asking for a dark got the shutter of a
            # light frame. It is the one per-exposure setting a plan legitimately decides --
            # unlike the amplifier and the gains, "is this a calibration frame" is exactly the
            # kind of thing the person writing the plan means to say.
            frame_type=settings.frame_type,
        )
        if response.failed:
            self.errors = response.errors or []
            return

        self.start_exposure(settings)

    def start_exposure(self, settings: SpecExposureSettings):
        """Start an exposure with the camera already configured.

        Split out of acquire() so a caller that has applied its own settings does not have
        them re-applied from the config on the way in.
        """
        if not self.detected:
            self.error("camera not detected")
            return

        if not self._initialized:
            self.error("not initialized")
            return

        self.errors = []

        self.latest_exposure_settings = settings
        self.debug(f"{function_name()}: latest_exposure_settings: {self.latest_exposure_settings}")

        self.start_activity(NewtonActivities.Acquiring)
        ret = self.sdk.StartAcquisition()
        if ret != atmcd_errors.Error_Codes.DRV_SUCCESS:
            self.error(f"could not StartAcquisition() (code={error_code(ret)})")
            return
        self.info("started exposure with sdk.StartAcquisition()")

        self.start_activity(NewtonActivities.Exposing)

    def readout(self):
        if not self.detected:
            self.error("camera not detected")
            return

        # self.debug(
        #     f"{function_name()} latest_exposure_settings: {self.latest_exposure_settings}"
        # )
        assert self.latest_exposure_settings and self.latest_exposure_settings.image_full_name
        Path(self.latest_exposure_settings.image_full_name).parent.mkdir(parents=True, exist_ok=True)

        self.start_activity(NewtonActivities.ReadingOut)
        # The SDK writes the file and `fits.setval` then rewrites its header, so both are
        # inside one protect: a ram->shared move must not see the file between them. This
        # also records it as a product, so release_folder() waits for it to reach the
        # shared area instead of discarding it as scratch.
        with MoveGuardian().protect(self.latest_exposure_settings.image_full_name):
            ret = self.sdk.SaveAsFITS(self.latest_exposure_settings.image_full_name, typ=0)
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.info(f"saved {self.latest_exposure_settings.image_full_name}")
                # Two cards, so a frame says both what was asked for and what happened.
                # EXPOSURE used to be overwritten with the REQUEST, which destroyed the only
                # record of the actual: the header could never disagree with the caller,
                # however the camera had quantised or clamped the request.
                requested = self.latest_exposure_settings.exposure_duration
                actual = self.actual_exposure_duration if self.actual_exposure_duration is not None else requested
                fits.setval(
                    self.latest_exposure_settings.image_full_name,
                    "EXPOSURE",
                    value=actual,
                    # 43 chars. A FITS card is 80 total, and this one spends 33 on
                    # `EXPOSURE= <20-wide value> / `, so a comment over 47 is truncated --
                    # astropy says so with a VerifyWarning at readout, which is easy to miss
                    # in a log. The original ran to 52 and every frame on the share carries
                    # "(GetAcquisitionTim" with the parenthesis unclosed.
                    comment="[s] actual exposure (GetAcquisitionTimings)",
                )
                fits.setval(
                    self.latest_exposure_settings.image_full_name,
                    "EXPREQ",
                    value=requested,
                    comment="[s] exposure requested",
                )
                # IMAGETYP is the FITS convention for this, and until now the frame type was
                # recorded NOWHERE in the file -- only in the filename, which is the first
                # thing lost to a copy or a rename. A calibration library sorted on the header
                # could not tell a dark from a light.
                fits.setval(
                    self.latest_exposure_settings.image_full_name,
                    "IMAGETYP",
                    value=self.latest_exposure_settings.frame_type.value,
                    comment="frame type requested (light/bias/dark/flat)",
                )
            else:
                self.error(f"failed sdk.SaveAsFITS({self.latest_exposure_settings.image_full_name}, typ=0) (ret={ret})")

        self.end_activity(NewtonActivities.ReadingOut)
        self.end_activity(NewtonActivities.Acquiring)

    def startup(self):
        if not self.conf.camera_enabled:
            self.info("Camera is disabled.")
            return
        if not self.detected:
            self.error("camera not detected")
            return
        self.start_activity(NewtonActivities.StartingUp)
        self.start_cooldown(target_set_point="regular")
        self._was_shut_down = False

    def shutdown(self):
        if not self.conf.camera_enabled:
            self.info("Camera is disabled.")
            return
        if not self.detected:
            self.error("camera not detected")
            return
        self.start_activity(NewtonActivities.ShuttingDown)
        self.start_warmup()
        self._was_shut_down = True

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(NewtonActivities.ShuttingDown)

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        while self.is_shutting_down:
            time.sleep(0.1)
        if self.is_on():
            self.power_off()

    def abort(self):
        if not self.detected:
            self.error("camera not detected")
            return
        if self.is_active(NewtonActivities.Exposing):
            ret = self.sdk.AbortAcquisition()
            if ret == atmcd_errors.Error_Codes.DRV_SUCCESS:
                self.end_activity(NewtonActivities.Exposing)
                self.debug("Aborted acquisition")
            else:
                self.error(f"Could not AbortAcquisition() (code={error_code(ret)})")

    def get_temperature(self) -> float | None:
        if not self.detected:
            self.error("camera not detected")
            return
        if not self._initialized:
            self.error("SDK not initialized")
            return

        (ret, temp) = self.sdk.GetTemperatureF()
        if ret == atmcd_errors.Error_Codes.DRV_TEMP_STABILIZED:
            return temp
        elif ret == atmcd_errors.Error_Codes.DRV_TEMP_NOT_STABILIZED:
            return None
        else:
            self.error(f"Could not GetTemperatureF() (code={error_code(ret)})")
            return None

    @property
    def temperature_is_stabilized(self) -> bool:
        if not self.detected:
            self.error("camera not detected")
            return False
        if not self._initialized:
            self.error("SDK not initialized")
            return False

        (ret, _temp) = self.sdk.GetTemperatureF()
        if ret == atmcd_errors.Error_Codes.DRV_TEMP_STABILIZED:
            return True
        elif ret == atmcd_errors.Error_Codes.DRV_SUCCESS or ret == atmcd_errors.Error_Codes.DRV_TEMPERATURE_NOT_STABILIZED:
            return False
        else:
            self.error(f"Could not GetTemperatureF() (code={error_code(ret)})")
            return False

    def __del__(self):
        self._terminated = True
        if self.detected:
            self.sdk.SetDriverEvent(0)
            self.sdk.ShutDown()

    def status(self) -> NewtonStatus:
        ret = NewtonStatus(
            detected=self.detected,
            powered=self.is_on(),
            connected=self.connected,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            current_set_point=self.set_point,
            regular_set_point=self.conf.temperature.regular_set_point if self.conf.temperature else None,
            science_set_point=self.conf.temperature.science_set_point if self.conf.temperature else None,
            temperature=self.get_temperature() if self.connected else None,
            errors=self.errors,
            latest_spec_exposure_settings=self.latest_exposure_settings,
        )

        return ret

    def can_expose(self) -> list[str]:
        ret = []
        if not self.detected:
            ret.append("not-detected")
        if not self._initialized:
            ret.append("not initialized")
        temp = self.get_temperature()
        if temp is not None and temp > self.TargetTemp:
            ret.append(f"temperature ({temp=} above {self.TargetTemp}")
        return ret

    def show_camera(self):
        assert self.power_switch is not None
        return {
            "SN": self.serial_number,
            "x_pixels": self.x_pixels,
            "y_pixels": self.y_pixels,
            "horizontal_binning": self.horizontal_binning,
            "vertical_binning": self.vertical_binning,
            "acquisition_mode": self.acquisition_mode,
            "set_point": self.set_point,
            "read_mode": self.read_mode,
            "em_gain": self.em_gain,
            "exposure_duration": self.exposure_duration,
            "activate_cooler": self.activate_cooler,
            "cooler_mode": self.cooler_mode,
            "power": {
                "switch": self.power_switch.ipaddr,
                "outlet": self.outlet_names[0],
                "state": "ON" if self.is_on() else "OFF",
            },
        }

    def camera_modes(self) -> dict:
        return {
            "exposure_duration": self.exposure_duration,
            "acquisition_mode": self.acquisition_mode,
            "read_mode": self.read_mode,
            "horizontal_binning": self.horizontal_binning,
            "vertical_binning": self.vertical_binning,
            "em_gain": self.em_gain,
            "set_point": self.set_point,
            "save": False,
        }

    def error(self, msg: str):
        self.logger.error(f"{self.log_label}: {msg}")
        self.errors.append(msg)

    def warning(self, msg: str):
        self.logger.warning(f"{self.log_label}: {msg}")
        self.errors.append(msg)

    def info(self, msg: str):
        self.logger.info(f"{self.log_label}: {msg}")

    def debug(self, msg: str):
        self.logger.debug(f"{self.log_label}: {msg}")

    # Parameter ORDER is the grouping. OpenAPI has no notion of a parameter group and
    # Swagger UI renders one flat table, but it emits them in signature order and renders
    # markdown in each description -- so adjacency plus a bold heading on each group's
    # first parameter is as close to sections-with-separators as the format allows.
    # Reordering is safe: the only Python caller is Highspec.do_autofocus, by keyword.
    def expose_single_image(
        self,
        # --- Exposure ---
        exposure_duration: Annotated[
            float | None,
            Query(
                description=(
                    "**--- Exposure ---**\n\n"
                    "Exposure length (seconds). Omit to use the configured duration.\n\n"
                    "`0` asks the camera for the shortest exposure its current readout "
                    "configuration allows -- the SDK takes 'the nearest valid value not less than "
                    "the given value' -- and the `EXPOSURE` header card reports what that turned "
                    "out to be. `frame_mode=bias` does this for you."
                ),
                # Was 0.001, which made a zero-length integration unrequestable: the endpoint
                # rejected the one duration a bias frame wants before the camera ever saw it.
                ge=0,
                le=3600,
            ),
        ] = None,
        delay_before_exposure: Annotated[
            float,
            Query(description="Delay before starting the exposure (seconds)."),
        ] = 0,
        frame_mode: Annotated[
            FrameType,
            Query(
                description=(
                    "What kind of frame this is. `bias` and `dark` hold the shutter **closed** "
                    "for the whole exposure; `light` and `flat` leave it on the camera's "
                    "automatic mode. Recorded in the FITS `IMAGETYP` card, and appended to the "
                    "filename for anything other than `light`.\n\n"
                    "Note that `bias` here means only 'shutter closed'. A true bias is also a "
                    "zero-length integration, and this endpoint does not shorten the exposure "
                    "-- ask for the shortest duration the camera accepts as well."
                )
            ),
        ] = FrameType.LIGHT,
        # --- Amplifier and readout ---
        #
        # None means "use the configured value". These used to carry concrete defaults,
        # which are indistinguishable from a caller's choice -- so the endpoint silently
        # overrode the config on every call, and editing the config had no effect here.
        amplifier_mode: Annotated[
            NewtonAmplifierMode | None,
            Query(
                description=(
                    "**--- Amplifier and readout ---**\n\n"
                    "`em` reads out through the electron-multiplying register; `conventional` "
                    "bypasses it. Decides whether `em_gain` below does anything. "
                    "Omit to use the configured mode."
                )
            ),
        ] = None,
        horizontal_shift_speed: Annotated[
            NewtonHSSpeed | None,
            Query(
                description=(
                    "Readout (horizontal shift) speed. Applies in both amplifier modes, and "
                    "together with `amplifier_mode` decides which `pre_amp_gain` values the "
                    "camera offers. Omit to use the configured speed."
                )
            ),
        ] = None,
        pre_amp_gain: Annotated[
            NewtonPreAmpGain | None,
            Query(
                description=(
                    "Pre-amplifier gain. Applies in **both** amplifier modes. Which values are "
                    "legal depends on `amplifier_mode` and `horizontal_shift_speed` together: "
                    "the camera is asked (IsPreAmpGainAvailable) before anything is applied, and "
                    "an unavailable combination is refused whole rather than applied in part. "
                    "Omit to use the configured gain."
                )
            ),
        ] = None,
        # --- EM mode only ---
        em_gain: Annotated[
            int | None,
            Query(
                description=(
                    "**--- EM mode only ---**\n\n"
                    "Gain of the electron-multiplying register. **Applied only when "
                    "`amplifier_mode` is `em`** -- in `conventional` mode it is accepted and "
                    "silently ignored. The 1..255 bound is the range of EM gain mode 0, which "
                    "is the mode this endpoint sets; it is not the camera's own advertised "
                    "range. Omit to use the configured gain."
                ),
                ge=1,
                le=255,
            ),
        ] = None,
        # --- Safety ---
        bypass_temperature_stabilization_check: Annotated[
            bool,
            Query(
                description=(
                    "**--- Safety ---**\n\n"
                    "Expose even if the sensor has not reached its target temperature. "
                    "Not recommended: the dark current will not be what the calibrations assume."
                )
            ),
        ] = False,
        image_full_path: Annotated[Path | None, Query(include_in_schema=False)] = None,
    ) -> CanonicalResponse:

        if not bypass_temperature_stabilization_check and not self.temperature_is_stabilized:
            return CanonicalResponse(errors=["Cannot start exposure while temperature is not stable"])

        # Each of these falls back to the configured value. The endpoint is where a human
        # overrides the site's choice for one exposure; the config is what everything else
        # runs on, including plans.
        exposure_duration = exposure_duration if exposure_duration is not None else self.conf.exposure_duration
        # After the config fallback, deliberately: a bias asks for the floor whether the
        # duration came from this call or from the site's configuration, and the configured
        # duration is the one a caller who names only `frame_mode=bias` would otherwise get.
        requested_duration = exposure_duration
        exposure_duration = integration_duration_for(frame_mode, exposure_duration)
        if exposure_duration != requested_duration:
            self.info(
                f"frame type is {frame_mode.value}: asking the camera for its shortest exposure, "
                f"not the {requested_duration} seconds resolved from the request and config"
            )
        amplifier_mode = amplifier_mode if amplifier_mode is not None else self.conf.amplifier_mode
        em_gain = em_gain if em_gain is not None else self.conf.em_gain
        pre_amp_gain = pre_amp_gain if pre_amp_gain is not None else _pre_amp_gain_by_index[self.conf.pre_amp_gain]
        horizontal_shift_speed = (
            horizontal_shift_speed if horizontal_shift_speed is not None else self.conf.horizontal_shift_speed
        )

        if delay_before_exposure > 0:
            self.debug(f"Delaying {delay_before_exposure} seconds before starting the exposure")
            time.sleep(delay_before_exposure)

        response = self._apply_exposure_settings(
            frame_type=frame_mode,
            exposure_duration=exposure_duration,
            amplifier_mode=amplifier_mode,
            em_gain=em_gain,
            pre_amp_gain=pre_amp_gain,
            horizontal_shift_speed=horizontal_shift_speed,
            binning=self.conf.binning if self.conf.binning is not None else NewtonBinning(x=1, y=1),
            roi=self.conf.roi,
            shutter=self.conf.shutter,
            acquisition_mode=self.conf.acquisition_mode,
        )
        if response.failed:
            return response

        # image Path
        # Only a path this call invented is this call's to move and release. When the caller
        # supplied one -- Highspec.do_autofocus does -- the folder belongs to that flow, and
        # releasing it here would reap it while the caller is still exposing into it.
        owns_folder = image_full_path is None
        if image_full_path is None:
            image_full_path = Path(PathMaker().make_spec_exposures_folder(spec_name="highspec") + "/image.fits")
        # `frame_mode != FrameType.LIGHT`, comparing enum to enum. This read
        # `!= NewtonFrameType.Light.value` -- a member against a plain string. StrEnum makes
        # that work by accident, which is why it went unnoticed, but it stops working the
        # moment the enum is not a StrEnum.
        if frame_mode != FrameType.LIGHT:
            image_full_path = image_full_path.with_name(
                image_full_path.stem + f"_{frame_mode.value}" + image_full_path.suffix
            )
        # self.info(f"image will be saved to '{image_full_path}'")

        # start_exposure, not acquire: the settings above are this call's, resolved from its
        # parameters, and acquire would re-apply the configured ones over the top -- which is
        # exactly what made every parameter here except exposure_duration a no-op.
        self.start_exposure(
            SpecExposureSettings(
                image_full_name=str(image_full_path),
                exposure_duration=exposure_duration,
                # Carried so readout() can write IMAGETYP. Omitted, these settings would
                # default to LIGHT and every frame this endpoint takes would claim to be one,
                # including the ones it had just closed the shutter for.
                frame_type=frame_mode,
            )
        )

        if owns_folder:
            self._start_exposure_mover(str(image_full_path))

        # The actual duration is returned, not just written to the header, so a caller
        # driving this endpoint learns the camera quantised or clamped its request without
        # having to open the file.
        return CanonicalResponse(
            value={
                "image": str(image_full_path),
                "exposure_duration": {
                    "requested": exposure_duration,
                    "actual": self.actual_exposure_duration,
                },
            }
        )

    def _start_exposure_mover(self, saved_path: str) -> None:
        """Move a single exposure to the shared area, and release its folder, once it lands.

        `expose_single_image` returns as soon as the exposure is started, so this waits on a watcher
        thread. Polling `Acquiring` is safe here because `acquire()` set it on the *caller's*
        thread before returning -- unlike a flag a worker sets later, it cannot be read
        before it is set. (Deepspec's band threads are the counter-example: see #37.)
        """
        folder = str(Path(saved_path).parent)

        def wait_then_hand_over():
            while self.is_active(NewtonActivities.Acquiring):
                time.sleep(0.5)
            Filer().move_ram_to_shared(saved_path)
            MoveGuardian().release_folder(folder, logger=logger)

        threading.Thread(name="newton-single-exposure-mover", target=wait_then_hand_over).start()

    # def set_camera_modes(
    #     self,
    #     exposure_duration: float = Query(
    #         description="Exposure length (seconds)", default=NewtonEMCCD.defaults.exposure_duration
    #     ),
    #     acquisition_mode: acquisition_modes = Query(
    #         description="Select a pre-defined acquisition modes",
    #         default=defaults.acquisition_mode,
    #     ),
    #     read_mode: read_modes = Query(
    #         description="Select a pre-defined read mode", default=defaults.read_mode
    #     ),
    #     set_point: int | None = Query(
    #         default=defaults.temperature.set_point if defaults.temperature else None,
    #         description="Target temperature",
    #     ),
    #     em_gain: int = Query(default=defaults.em_gain, ge=1, le=4095),
    #     horizontal_binning: int = Query(
    #         default=defaults.binning.x,  # type: ignore
    #         ge=1,
    #         le=1600,  # type: ignore
    #     ),
    #     vertical_binning: int = Query(default=defaults.binning.y, ge=1, le=400),  # type: ignore
    #     cooler_mode: cooler_modes | None = Query(
    #         default=defaults.temperature.cooler_mode if defaults.temperature else None,
    #         description="What to do about temperature at shutdown?",
    #     ),
    #     save: bool = Query(
    #         description="Save these settings as defaults?", default=False
    #     ),
    # ):
    #     self.set_modes(
    #         exposure_duration=exposure_duration,
    #         acquisition_mode=getattr(AcquisitionMode, acquisition_mode.value),
    #         read_mode=getattr(ReadMode, read_mode.value),
    #         set_point=set_point,
    #         cooler_mode=getattr(CoolerMode, cooler_mode.name if cooler_mode else ""),
    #         em_gain=em_gain,
    #         horizontal_binning=horizontal_binning,
    #         vertical_binning=vertical_binning,
    #         save=save,
    #     )

    #     @property
    #     def api_router(self) -> APIRouter:
    #         router = APIRouter()
    #         base_path = Const().BASE_SPEC_PATH + "highspec/camera"
    #         tag = "HighSpec Camera"

    #         router.add_api_route(base_path, tags=[tag], endpoint=self.show_camera)
    #         # router.add_api_route(base_path + '/expose', tags=[tag], endpoint=self.expose)
    #         router.add_api_route(
    #             base_path + "/status", tags=[tag], endpoint=self.status
    #         )
    #         router.add_api_route(
    #             base_path + "/set-modes", tags=[tag], endpoint=self.set_camera_modes
    #         )
    #         router.add_api_route(
    #             base_path + "/startup", tags=[tag], endpoint=self.startup
    #         )
    #         router.add_api_route(
    #             base_path + "/shutdown", tags=[tag], endpoint=self.shutdown
    #         )
    #         router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)

    #         return router


if __name__ == "__main__":
    camera = NewtonEMCCD()

    camera.startup()
    while camera.is_active(NewtonActivities.StartingUp):
        camera.logger.debug("waiting for NewtonActivities.StartingUp to end ...")
        time.sleep(5)
    from common.utils import time_stamp

    camera.acquire(
        SpecExposureSettings(
            exposure_duration=5,
            number_of_exposures=1,
            image_full_name=f"c:/tmp/newton_exposure_{time_stamp()}.fits",
        )
    )
    while camera.is_active(NewtonActivities.Exposing):
        camera.logger.debug("waiting for NewtonActivities.Exposing to end ...")
        time.sleep(5)

    print("done")
