import datetime
import os
import sys
import threading
import time
from collections.abc import Callable
from enum import IntEnum
from typing import ClassVar, get_args

import humanfriendly
import numpy as np
from astropy.io import fits
from astropy.io.fits import Card
from pydantic import BaseModel

from cameras.greateyes import stale_session
from common.activities import GreatEyesActivities
from common.config import Config
from common.config.greateyes import GreateyesSettingConfig
from common.dlipowerswitch import SwitchedOutlet
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.assignments import SpectrographAssignment
from common.models.deepspec import DeepspecSettings
from common.models.greateyes import (
    Gain,
    GreateyesSettingsModel,
    ReadoutSpeed,
)
from common.models.statuses import GreateyesStatus
from common.networking import NetworkedDevice
from common.spec import (
    CLOSED_SHUTTER_FRAMES,
    DeepspecBands,
    FrameType,
    SpecExposureSettings,
    integration_duration_for,
)
from common.utils import OperatingMode, RepeatTimer, function_name

sys.path.append(os.path.join(os.path.dirname(__file__), "sdk"))
import cameras.greateyes.sdk.greateyesSDK as ge  # noqa: N813

logger = get_logger(__name__)
dll_version = ge.GetDLLVersion()
shown_dll_version = False

if not shown_dll_version:
    logger.info(f"Greateyes DLL version: '{dll_version}'")
    shown_dll_version = True

FAILED_TEMPERATURE = -300

# ge.Status value 11, 'cooling turned off' -- the camera reporting that it has reset its own
# cooling control because the TEC backside got too hot. SDK manual 5.5.6, and the note under
# TemperatureControl_GetTemperature in 5.4.3. Not a value we ever set.
COOLING_TURNED_OFF_STATUS = 11

# The vendor's TEC backside limit, from the SDK header for TemperatureControl_GetTemperature:
# "Maximum backside temperature is about 55 [degrees] C." Ours, not the camera's -- the camera
# has its own threshold and announces crossing it as COOLING_TURNED_OFF_STATUS, which is a
# separate and more authoritative signal. This one is the software's earlier, cruder check.
MAX_BACKSIDE_TEMPERATURE = 55

# How long abort() waits for ge.StopMeasurement to actually make the DLL idle before saying
# it did not. Bounds the report, not the camera: nothing here can force a stop, so the value
# only decides how quickly a stop that did not take is called out.
STOP_MEASUREMENT_TIMEOUT_SECONDS = 5


# ge.Status 1, whose wrapper string is 'no camera detected' (the SDK manual's table calls it
# "No camera connected"). Reported by TemperatureControl_GetTemperature when the session is
# gone rather than when a single read failed -- observed on band G, 2026-09-08, after its
# camera was rebooted underneath the service.
NO_CAMERA_STATUS = 1

# Consecutive failed reads reporting NO_CAMERA_STATUS before `detected` comes down.
#
# Not 1. ge.Status is the global shared by four band threads (MAST_spec#87), and demoting is
# not free: the probe that follows calls try_connect_camera, which disconnects and
# re-establishes the session. Doing that to a healthy camera on one stray read is worse than
# waiting. Requiring a run makes a cross-thread coincidence have to repeat, and the cost is
# latency measured against the 30 s backside poll, against a failure that currently persists
# until someone restarts the service.
NO_CAMERA_READS_BEFORE_DEMOTING = 3


def sdk_status() -> str:
    """The SDK's status word as of the call that has just returned.

    Only meaningful when read immediately after a wrapper that calls UpdateStatus(), with no
    other SDK call in between -- 19 of the 50 wrappers never touch it, and after one of those
    this reports some earlier call's status instead (MAST_spec#94, #98).

    Carries the MAST_spec#87 caveat: ge.Status is a module global shared by all four band
    threads, so a busy moment can return another band's. Fine for a log line, not something
    to branch on without weighing what a wrong read would cost.
    """
    return f"status={ge.Status} '{ge.StatusMSG}'"


FITS_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class CropSettingsModel(BaseModel):
    col: int
    line: int
    enabled: bool


class BytesPerPixel(IntEnum):
    Two = 2
    Three = 3
    Four = 4


# The database and the API say "low"/"high"; ge.SetupGain takes an int. The pairing is the
# vendor's, from the SDK header for SetupGain:
#   0 -> Low ( Max. Dyn. Range )
#   1 -> Std ( High Sensitivity )
# Unused while the SetupGain call in start_exposure is commented out -- these cameras reject
# both values. Kept with it, so re-enabling that one line needs nothing else.
_gain_index = {
    Gain.low: 0,
    Gain.high: 1,
}


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
        # UTC is captured, local is derived from it. The other way round -- now() then
        # .astimezone(UTC) -- asks a NAIVE datetime to convert itself, which silently means
        # "assume this machine's timezone": right here today, wrong and unrecoverable if a
        # machine's TZ is off, because the record never said which zone it meant. That is
        # the mistake MAST_common#28 records for the observing-night folders.
        self.timing.start_utc = datetime.datetime.now(datetime.UTC)
        self.timing.start = self.timing.start_utc.astimezone()

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
        # Narrowed once, here, so nothing downstream repeats the assert. `conf` keeps what is
        # not a setting -- network, device, enabled -- which is why this class needs both and
        # cannot bind conf straight to the settings the way NewtonEMCCD does.
        #
        # This was a GreateyesSettingsModel converted from the config at startup (twice,
        # identically), so the class carried two objects holding the same values and read
        # from both: the model for temp and probing, the config for the per-exposure
        # fallbacks. The config is the one that is authoritative -- the model is the API
        # shape, where every field is optional because a caller may omit it.
        self.settings: GreateyesSettingConfig = self.conf.settings
        self.latest_spec_exposure_settings: SpecExposureSettings | None = None
        self.latest_greateyes_exposure_settings: GreateyesSettingsModel | None = None
        # Path save_image() actually wrote, which may differ from the requested image_file.
        self.latest_saved_image_path: str | None = None
        # Whether the last exposure left the shutter to the camera. False until one runs, so
        # a stray close is a no-op rather than an attribute error.
        self.latest_shutter_automatic: bool = False

        self.ge_device = self.conf.device
        self._name = f"Deepspec-{self.band}"
        self.outlet_name = f"Deepspec{self.band}"
        self.errors = []
        self.output_modes: list[str] = []
        self.sensor_temperature_target: float | None = None
        # Consecutive failed temperature reads that reported NO_CAMERA_STATUS. Reset by any
        # successful read and by any failure reporting a different status, so only an
        # unbroken run demotes.
        self.consecutive_no_camera_reads = 0

        from common.dlipowerswitch import OutletDomain, SwitchedOutlet

        assert self.conf.network is not None
        NetworkedDevice.__init__(self, self.conf.model_dump())
        SwitchedOutlet.__init__(self, outlet_name=f"{self.outlet_name}", domain=OutletDomain.SpecOutlets)

        self.enabled = self.conf.enabled

        self.acquisition: str | None = None

        # time.monotonic() readings, not wall-clock: both are only ever used as "how long
        # since", and the wall clock moves (DST, NTP).
        self.last_backside_temp_check: float | None = None
        self.backside_temp_safe = True
        # time.monotonic() when on_timer first saw DllIsBusy true, so the busy->idle edge can
        # report how long it lasted. None while the DLL is idle.
        self.dll_busy_since: float | None = None
        # time.monotonic() when abort() asked the SDK to stop a measurement, so on_timer can
        # bound the wait for it to take effect. Monotonic like its neighbours above: this is
        # only ever read as "how long since", and timings[].start_time is a wall-clock
        # datetime that cannot be subtracted from time.monotonic().
        self.stopping_measurement_since: float | None = None

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

        self.last_probe_time: float | None = None  # time.monotonic(), see on_timer
        self.timer_frequency: float = 1  # [seconds] how often to check the camera status, e.g. backside temperature
        self.timer = RepeatTimer(self.timer_frequency, function=self.on_timer)
        self.timer.name = f"deepspec-camera-{self.band}-timer-thread"
        self.timer.start()

    def try_connect_camera(self, after_power_cycle: bool = False):
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
        except Exception as e:  # noqa: BLE001 -- the vendor's SDK sometimes throws an exception here, even when the camera is disconnected
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
            # No msg=ge.StatusMSG here. ConnectToSingleCameraServer is one of the 19 SDK
            # wrappers that never call UpdateStatus(), so on failure StatusMSG still holds
            # whatever the last call that DID set it left there -- which is the
            # SetupCameraInterface immediately above, whose success message is
            # 'camera detected and ok'. Every failed connect therefore used to report a
            # success beside its own failure, deterministically and with no thread involved.
            # That line was read as proof of a cross-thread status race (MAST_spec#87) and
            # cost the fleet the serialisation attempt in #88. MAST_spec#94.
            self.error(
                f"could not ge.ConnectToSingleCameraServer(addr={self.ge_device}) ipaddr='{self.network.ipaddr}' "
                + f"(ret={ret})"
            )
            # Read-only, next to the failure it explains: says whether a live process on this
            # machine is holding the camera (MAST_spec#77, killable) or whether the session is
            # stranded at the camera end (nothing here to kill). Kills nothing itself -- the
            # point is to learn which of the two actually dominates before acting on either.
            # Only on the failure path, so a healthy machine never logs it.
            for line in stale_session.describe(self.network.ipaddr, after_power_cycle=after_power_cycle):
                self.warning(line)
            self.end_activity(GreatEyesActivities.Probing, label=self.name)
            return
        # self.debug(
        #     f"OK: ge.ConnectToSingleCameraServer(addr={self.ge_device}) "
        #     + f"(ret={ret})"   # no msg=: this call does not set StatusMSG (MAST_spec#94)
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
            f"OK: ge.ConnectCamera(model={model}, ipaddr='{self.network.ipaddr}' addr={self.ge_device} fw={fw_version}) "
            f"(ret={ret}, msg='{ge.StatusMSG}')"
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

        if not self.detected:
            # Whether the camera was actually power-cycled before the second attempt. It
            # changes what an accepting server port means, so the diagnosis needs to know:
            # after a cycle it is a camera that has finished booting, not a stranded session.
            power_cycled = False
            if self.power_switch is not None and self.power_switch.detected:
                power_cycled = True
                if self.is_off():
                    self.info("powering ON")
                    self.power_on()
                else:
                    self.info("cycling power")
                    self.cycle()
                boot_delay = self.settings.probing.boot_delay
                self.info(f"waiting for the camera to boot ({boot_delay} seconds) ...")
                assert boot_delay
                time.sleep(boot_delay)
                # Polling the camera's server port instead of sleeping was tried on
                # 2026-09-07 and backed out the same day. It is kept here, commented, because
                # what it measured is worth having and the replacement is a one-line swap:
                #
                #     waited = stale_session.wait_until_accepting(self.network.ipaddr, boot_delay)
                #
                # What it showed: boot_delay is too SHORT, not too long. The camera was still
                # not listening 28.5 s after the power cycle and was listening by 49.7 s, so
                # the SDK connect fired into a dead port, blocked 21 s, failed, and recovery
                # waited for the next 60 s probe cycle -- 61 s to bring the array up against
                # 48 s for the run before it.
                #
                # Why it is not simply re-enabled with a bigger budget: that run is also the
                # only one in which we opened ~12 TCP connections to the camera while it was
                # booting, and it is the slowest recorded. At n=1 a disturbed boot cannot be
                # told apart from ordinary variance, and this is a live instrument. If it is
                # picked up again, sleep boot_delay untouched first and poll only BEYOND it,
                # so the boot window stays exactly as it is today.
            else:
                self.warning(
                    f"power switch {self.power_switch} not detected, skipping power cycle, will try to connect "
                    + "to the camera anyway"
                )

            self.try_connect_camera(after_power_cycle=power_cycled)
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
            + f"sensor temp range={self.min_temp}C to {self.max_temp}C"
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
                details=[f"to {target_temperature}C"],
            )
        else:
            self.append_error(
                f"FAILED to set temperature to {target_temperature}C with ge.TemperatureControl_SetTemperature "
                f"(status: {ge.StatusMSG} ({ge.Status}))"
            )

    def cool_down(self):
        if not self.detected:
            return

        assert self.ge_device is not None

        target_temp = self.settings.temp.target_cool
        self.sensor_temperature_target = target_temp
        if self._apply_setting(ge.TemperatureControl_SetTemperature, target_temp):
            self.start_activity(
                GreatEyesActivities.CoolingDown,
                label=self._name,
                details=[f"to {target_temp}C"],
            )

    def retreat_to_room_temperature(self, reason: str):
        """Back the cooling off to room temperature after a TEC over-temperature.

        This is what greateyes ask for, and it is not SwitchOff. The SDK manual's note on
        TemperatureControl_GetTemperature (5.4.3):

            If the function returns statusMSG = 11, the camera resets cooling control
            because the TEC backside temperature is getting too high. In this case you
            should set coolingLevel to room temperature [...]

        So the camera protects itself -- status 11 is 'cooling turned off', reported by the
        camera, not requested by us -- and our job is to stop asking for a set point it has
        already abandoned. Switching off instead would work, but it leaves the control in a
        state the vendor does not describe, and it is not what the note says to do.

        Idempotent: while the backside stays hot this runs once per check_interval, and the
        log line each time is the point.
        """
        self.backside_temp_safe = False
        room_temperature = self.max_temp if self.max_temp is not None else 20
        self.error(f"cooling backed off to {room_temperature}C ({reason}); contact greateyes GmbH")

        if not self._apply_setting(ge.TemperatureControl_SetTemperature, room_temperature):
            self.append_error(f"FAILED to back the cooling off to {room_temperature}C")

        # Nothing is going to reach the cold set point now, so stop waiting for it rather
        # than leaving CoolingDown set for the rest of the run.
        self.end_activity(GreatEyesActivities.CoolingDown, label=self._name)
        self.sensor_temperature_target = None

    def warm_up(self):
        if not self.detected:
            return
        assert self.ge_device is not None

        target_temp = self.max_temp if self.max_temp is not None else 20
        if self._apply_setting(ge.TemperatureControl_SetTemperature, target_temp):
            self.start_activity(
                GreatEyesActivities.WarmingUp,
                label=self._name,
                details=[f"to {target_temp}C"],
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

        # A bias integrates for nothing, so the duration stops being the caller's once the
        # frame type says bias. Resolved once, here, and used for both the SDK call and the
        # settings the status surface and the header report -- computing it twice is how the
        # two come to disagree.
        requested_duration = greateyes_exposure_settings.exposure_duration
        exposure_duration = integration_duration_for(greateyes_exposure_settings.frame_type, requested_duration)
        if exposure_duration != requested_duration:
            self.info(
                f"frame type is {greateyes_exposure_settings.frame_type.value}: "
                f"integrating for {exposure_duration} s, not the {requested_duration} s requested"
            )
            greateyes_exposure_settings.exposure_duration = exposure_duration

        self.latest_spec_exposure_settings = SpecExposureSettings(
            exposure_duration=exposure_duration,
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
        conf = self.settings

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
                    f"image size changed after setting output mode: was "
                    f"{self.x_size} x {self.y_size} x {self.bytes_per_pixel}, "
                    f"now {info[0]} x {info[1]} x {info[2]}"
                )
                self.x_size = info[0]
                self.y_size = info[1]
                self.bytes_per_pixel = info[2]

        # The two sides spell the speed differently: the exposure model holds a ReadoutSpeed
        # enum, the config a plain int (both are kHz). `.value` on the config side would
        # have been an AttributeError on a fallback nothing had taken yet.
        readout_speed = (
            greateyes_exposure_settings.readout.speed.value
            if greateyes_exposure_settings.readout and greateyes_exposure_settings.readout.speed is not None
            else conf.readout.speed
        )
        self._apply_setting(ge.SetReadOutSpeed, readout_speed)

        # conf.binning is Optional on the model, though its validator fills it in; state the
        # fallback rather than relying on that.
        binning = greateyes_exposure_settings.binning or conf.binning
        self._apply_setting(
            ge.SetBinningMode,
            (binning.x if binning is not None else 1, binning.y if binning is not None else 1),
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

        # "low"/"high" everywhere the humans are -- database, endpoint, /docs -- and the
        # SDK's integer only here, via _gain_index. Applied only when someone actually asked
        # for one: the exposure, or failing that the site config.
        #
        # THESE CAMERAS HAVE NO GAIN CONTROL. Tested on all four bands, 2026-09-07, both of
        # the vendor's only two documented values:
        #
        #   SetupGain(0) -> False, status 'camera detected and ok (0)'
        #   SetupGain(1) -> False, status 'one ore more parameters are out of range (8)'
        #
        # GE 1024 1024 BI DD rejects both, so this is an unsupported feature rather than a
        # wrong argument or a bad mapping. Nine nights of archived logs agree: 34 attempts,
        # 34 failures, no success ever -- all of them SetupGain(0), whose status reads
        # 'camera detected and ok' because the DLL leaves the status word untouched on that
        # path. Only trying (1), which does set it, made the failure legible.
        #
        # Commented rather than deleted: if a future model or firmware supports it, this is
        # the line and it is already correct. The warning replaces it because the previous
        # behaviour was to log FAILED and expose anyway -- so every frame was taken at the
        # camera's default gain, with nothing on the product saying so.
        gain = greateyes_exposure_settings.gain if greateyes_exposure_settings.gain is not None else conf.gain
        if gain is not None:
            # self._apply_setting(ge.SetupGain, _gain_index[gain])
            self.warning(
                f"gain '{gain}' was requested but these cameras have no gain control; exposing at the camera's "
                "default. Remove 'gain' from the deepspec settings to stop asking (MAST_spec#98)."
            )

        self.end_activity(GreatEyesActivities.SettingParameters, label=self.name)

        # `is not None`, not truthiness. A zero duration -- which is exactly what a bias asks
        # for -- is falsy, so the bare assert refused the one case this camera now has to
        # support, raising before SetExposure was ever reached. Same shape as the gain
        # fallback that treated low-gain-0 as "unset".
        #
        # SetExposure takes whole milliseconds, documented range [0..2^31], so 0 is a legal
        # request meaning "your floor". What the camera then integrates for is not reported
        # by this SDK: there is no GetAcquisitionTimings equivalent, and GetLastMeasTimeNeeded
        # returns exposure PLUS readout, so it cannot answer the question either.
        assert self.latest_exposure.settings.exposure_duration is not None
        if not self._apply_setting(ge.SetExposure, int(self.latest_exposure.settings.exposure_duration * 1000)):
            self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            return

        # Both of these decide the same thing and have to agree, or the shutter stays shut.
        # OpenShutter(2) puts the camera in automatic mode -- "TTL High while image
        # acquisition", per the SDK header -- but the acquisition is what drives it, and
        # StartMeasurement_DynBitDepth's `showShutter` parameter ("use auto shutter")
        # defaults to False. It was called with the defaults, so every automatic-shutter
        # exposure told the camera to open the shutter and then started a measurement that
        # would not: both calls returned OK and the shutter never moved.
        #
        # `shutter` is the resolved value, from the exposure's settings or the config. The
        # mode used to be read straight off greateyes_exposure_settings, which is not the
        # same thing -- the timings above already fall back to the config, so an assignment
        # arriving without a shutter got configured timings and then an AssertionError.
        # A bias or a dark is defined by the sensor seeing nothing, so the frame type decides
        # this before the shutter configuration gets a say. Without it, both of the branches
        # below leave the shutter OPEN through the integration -- 2 is automatic, 1 is
        # permanently open -- so a frame requested as `dark` was a light frame that had been
        # renamed. The type was recorded in the filename and nowhere else.
        frame_type = self.latest_exposure.settings.frame_type
        closed = frame_type in CLOSED_SHUTTER_FRAMES

        automatic = not closed and shutter is not None and shutter.automatic
        # Remembered for _close_shutter_if_manual, which has to close whatever this opened.
        # It used to re-derive the answer from the settings model alone, so an exposure that
        # ran in manual mode because the CONFIG said so was never closed: the model had no
        # shutter, the check read that as "nothing to do", and the shutter stayed open.
        self.latest_shutter_automatic = automatic
        # 0 closed, 1 open, 2 automatic. `closed` wins; otherwise this is unchanged.
        self._apply_setting(ge.OpenShutter, 0 if closed else (2 if automatic else 1))
        # showShutter has to agree with OpenShutter or the two cancel out -- that is the bug
        # the comment above records. For a closed frame both say "no shutter movement".
        ret = ge.StartMeasurement_DynBitDepth(showShutter=automatic, addr=self.ge_device)
        op = f"StartMeasurement_DynBitDepth(showShutter={automatic}, addr={self.ge_device})"
        if ret:
            self.info(f"OK - {op}")
            self.start_activity(GreatEyesActivities.Exposing, label=self.name)
            assert self.latest_exposure.timing
            # One clock reading, two spellings of it. These used to be two separate now()
            # calls, so start and start_utc differed by however long the second one took.
            self.latest_exposure.timing.start_utc = datetime.datetime.now(datetime.UTC)
            self.latest_exposure.timing.start = self.latest_exposure.timing.start_utc.astimezone()
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
        if self.latest_shutter_automatic:
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
            raise RuntimeError("empty self.latest_exposure.settings.image_file")

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

        # The frame is discarded, not written, if an abort arrived while this thread ran.
        # The data fetched above is valid -- the measurement completed before the readout
        # started -- which is exactly why nothing else would stop it reaching disk.
        if self.is_active(GreatEyesActivities.Aborting):
            self.info("aborted: discarding the frame instead of saving it")
            self.end_activity(GreatEyesActivities.Aborting, label=self.name)
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
        # IMAGETYP is the FITS convention, and the frame type was recorded NOWHERE in the
        # file until now -- only in the filename, which is the first thing lost to a copy or
        # a rename. TYPE above is a constant "RAW" describing the processing level, not this;
        # it is left alone because something downstream may read it.
        hdr.append(
            Card(
                "IMAGETYP",
                self.latest_exposure.settings.frame_type.value,
                "frame type requested (light/bias/dark/flat)",
            )
        )

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
        hdr.append(
            Card(
                "TEMPGOAL",
                self.settings.temp.target_cool,
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
        # NOT "BITPIX": that is a reserved FITS keyword, computed by astropy from the data
        # array, and a card of our own was silently discarded on the way out -- so this
        # never recorded anything. It was also the requested bytes-per-pixel rather than the
        # applied one, which since the model's default became None would have been a card
        # with no value at all.
        #
        # `self.bytes_per_pixel` is read back from the camera by GetImageSize() after the
        # output mode is set, so it is what the sensor is actually delivering.
        if self.bytes_per_pixel is not None:
            hdr.append(Card("BYTESPP", self.bytes_per_pixel, "bytes per pixel as reported by the camera"))
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
        except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
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

        # Close the guard BEFORE stopping the measurement, not after.
        #
        # on_timer starts a readout when it sees `Exposing` set and DllIsBusy false, and
        # StopMeasurement is exactly what makes DllIsBusy false. Clearing the flags afterwards
        # leaves a window -- one SDK call wide -- in which the 1 Hz timer thread can read both
        # conditions as true and read out the frame this call is aborting. That is the bug
        # MAST_spec#66 fixed on the slow path and left open on the fast one.
        #
        # end_activity mutates under self.lock and is_active reads under it, so once the flag
        # is down the guard cannot re-open.
        self.end_activity(GreatEyesActivities.Exposing, label=self.name)
        self.end_activity(GreatEyesActivities.Acquiring, label=self.name)

        # An abort during the READOUT is the case StopMeasurement cannot help with. The SDK
        # doc (section 5.5.4) says it "stops an ongoing measurement" -- but a readout only
        # starts once the measurement is over, so by then DllIsBusy is already false and the
        # call below is skipped. The data is valid and complete, and the readout thread will
        # write it. Only this flag stops that.
        #
        # Set unconditionally: abort has to be able to catch a readout already running.
        self.start_activity(GreatEyesActivities.Aborting, label=self.name)
        if not self.is_active(GreatEyesActivities.ReadingOut):
            self.end_activity(GreatEyesActivities.Aborting, label=self.name)

        if ge.DllIsBusy(addr=self.ge_device):
            # Raised BEFORE the call, so the clock starts even if StopMeasurement itself
            # blocks. on_timer's StoppingMeasurement block then polices what this call cannot
            # confirm on its own: whether the DLL actually went idle. It ends the flag when
            # DllIsBusy clears, and after 5 seconds says "stopping measurement takes too
            # long" if it has not.
            #
            # That block already existed and nothing ever raised this flag, so it had never
            # run -- a consumer waiting on a signal no code sent, which is the shape
            # common/CLAUDE.md warns about under the activity-flag conventions.
            #
            # Observed 2026-09-07, and the reason this matters: an abort on a wedged band U
            # logged `could not ge.StopMeasurement(addr=0)`, cleared the flags and returned.
            # DllIsBusy stayed true, so the camera could not expose again -- and reported
            # operational=True with no activity to say otherwise. The bounded wait turns that
            # into a logged failure at the moment it happens.
            #
            # Exposing and Acquiring stay cleared above rather than being left to that block:
            # MAST_spec#66 needs them down before StopMeasurement makes DllIsBusy false, or
            # the timer reads out the very frame this call is aborting. The block's own
            # end_activity calls for them are then no-ops -- end_activity returns early on a
            # flag that is not set -- so this polices the stop without reopening that window.
            self.stopping_measurement_since = time.monotonic()
            self.start_activity(GreatEyesActivities.StoppingMeasurement, label=self.name)

            ret = ge.StopMeasurement(addr=self.ge_device)
            if not ret:
                self.append_error(f"could not ge.StopMeasurement(addr={self.ge_device})")

    def on_timer(self):  # noqa: C901 -- a hardware state machine; the branching is the problem domain, not a failure to decompose
        """
        Called periodically by a timer.
        Checks if any in-progress activities can be ended.
        """

        # self.enabled, from conf.enabled -- the camera's own flag. This used to read
        # greateyes_settings.enabled, a field the SETTINGS config does not have, so the
        # model default of True applied and the check never fired.
        if not self.enabled:
            return

        if self.shutdown_event.is_set():
            self.timer.cancel()
            return

        assert self.ge_device is not None
        if (
            not self.is_active(GreatEyesActivities.Probing)
            and not self.detected
            and (self.last_probe_time is None or time.monotonic() - self.last_probe_time > self.settings.probing.interval)
        ):
            self.last_probe_time = time.monotonic()
            self.probe()
            return

        if not self.detected:
            return

        # monotonic, not wall-clock: these two are "how long since", and the wall clock is
        # not monotonic. Israel observes DST, so with datetime.now() the autumn fallback
        # makes `now - last` negative for an hour -- the probe and the temperature check
        # simply stop firing -- and the spring jump fires them an hour early. An NTP step or
        # a hand-set clock does the same on any day of the year.
        now = time.monotonic()

        # Watch DllIsBusy across ticks, so the camera coming back to its senses is recorded.
        #
        # A camera can get stuck with DllIsBusy true and no measurement that will ever end
        # (MAST_spec#104): it then refuses every exposure, while `detected`, `connected` and
        # `operational` all still read True. The only outward sign is that both temperature
        # getters return None, because they return None precisely when the DLL is busy.
        #
        # Recovery was invisible. Band G sat wedged from 15:43 on 2026-09-07 and was exposing
        # normally by 07:53 the next morning, same process, no power cycle -- and NOTHING was
        # logged in between, so the 16 hours is only an upper bound on a duration nobody can
        # recover from the record. By then abort's StoppingMeasurement flag was long ended,
        # so no activity remained to close and no line was written.
        #
        # The busy->idle edge is the one that matters, and its meaning depends on whether an
        # exposure was running. On the normal path Exposing is still set here: this tick's
        # later block is what ends it, further down. So an edge with Exposing down is a
        # camera that has released itself with nothing to release -- the #104 recovery.
        #
        # Read ONCE per tick and reused by every branch below that needs it. This tick's
        # decisions -- end Exposing and start the readout, resolve StoppingMeasurement,
        # decide whether a None temperature means "busy" or "failed" -- were each re-reading
        # it, so a single pass could act on up to five different answers and reason about
        # them as though they were one. One snapshot makes the tick self-consistent.
        #
        # The cost is that the view can be up to a tick stale, which is bounded by the timer
        # interval and is what a 1 Hz poll means anyway: the worst case is noticing an idle
        # DLL one second later. Nothing here starts a measurement, so the flag cannot go the
        # other way behind our back mid-tick.
        #
        # get_sensor_temperature and get_back_temperature keep their own reads: status()
        # calls them from the HTTP thread, where there is no tick and no snapshot to share.
        dll_busy = ge.DllIsBusy(addr=self.ge_device)
        if dll_busy and self.dll_busy_since is None:
            self.dll_busy_since = now
        elif not dll_busy and self.dll_busy_since is not None:
            busy_for = humanfriendly.format_timespan(now - self.dll_busy_since)
            if self.is_active(GreatEyesActivities.Exposing):
                self.debug(f"DllIsBusy cleared after {busy_for} (exposure finished)")
            else:
                self.warning(
                    f"DllIsBusy cleared after {busy_for} with no exposure in progress: "
                    f"the camera has come back on its own (MAST_spec#104)"
                )
            self.dll_busy_since = None
        if (
            self.last_backside_temp_check is None
            or (now - self.last_backside_temp_check) > self.settings.temp.check_interval
        ):
            ret = self.get_back_temperature()
            if ret is None:
                # self.error("failed to read back temperature")
                pass
            elif ret >= MAX_BACKSIDE_TEMPERATURE:
                self.backside_temp_safe = False
                self.error(f"back side temperature too high: {ret} degrees celsius (limit {MAX_BACKSIDE_TEMPERATURE})")
                self.retreat_to_room_temperature(f"backside temperature {ret}C, limit {MAX_BACKSIDE_TEMPERATURE}C")
            else:
                self.backside_temp_safe = True

            self.last_backside_temp_check = now

        if self.is_active(GreatEyesActivities.Exposing) and not dll_busy:
            self.end_activity(GreatEyesActivities.Exposing, label=self.name)

            # Computed in UTC and converted for the local twins, so the pair cannot disagree
            # about which instant it means. mid is the midpoint of the exposure, which is
            # the timestamp that matters scientifically.
            timing = self.latest_exposure.timing
            assert timing is not None
            timing.end_utc = datetime.datetime.now(datetime.UTC)
            timing.mid_utc = timing.start_utc + (timing.end_utc - timing.start_utc) / 2
            timing.end = timing.end_utc.astimezone()
            timing.mid = timing.mid_utc.astimezone()
            self.readout_thread = threading.Thread(
                name=f"deepspec-camera-{self.band}-readout-thread",
                target=self.readout,
            )
            self.readout_thread.start()

        if self.is_active(GreatEyesActivities.StoppingMeasurement):
            if not dll_busy:
                self.stopping_measurement_since = None
                self.end_activity(GreatEyesActivities.StoppingMeasurement, label=self.name)
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)
                self.end_activity(GreatEyesActivities.Acquiring, label=self.name)
            # `now` is time.monotonic(); this used to compare it against
            # timings[...].start_time, which is a datetime -- `float - datetime` is a
            # TypeError. Nothing ever raised StoppingMeasurement, so the branch never ran and
            # the error never surfaced. It would have surfaced HERE, in on_timer, on a
            # RepeatTimer that has no exception handling (MAST_common#106): the band's timer
            # thread would have died, and with it its probing, cooling and recovery. And it
            # would have happened in exactly the case this branch exists for -- a stop that
            # does not take.
            elif (
                self.stopping_measurement_since is not None
                and now - self.stopping_measurement_since > STOP_MEASUREMENT_TIMEOUT_SECONDS
            ):
                elapsed = now - self.stopping_measurement_since
                self.append_error(
                    f"stopping measurement takes too long "
                    f"({elapsed:.1f} > {STOP_MEASUREMENT_TIMEOUT_SECONDS} seconds); "
                    f"the camera is still busy and will refuse the next exposure"
                )
                self.stopping_measurement_since = None
                self.end_activity(GreatEyesActivities.StoppingMeasurement, label=self.name)
                self.end_activity(GreatEyesActivities.Exposing, label=self.name)
                self.end_activity(GreatEyesActivities.Acquiring, label=self.name)

        if self.is_active(GreatEyesActivities.AdjustingTemperature) and self.sensor_temperature_target is not None:
            sensor_temp = self.get_sensor_temperature()
            if sensor_temp is None:
                if not dll_busy:
                    self.append_error("failed reading sensor temperature")
            elif abs(sensor_temp - self.sensor_temperature_target) <= 1:
                self.end_activity(GreatEyesActivities.AdjustingTemperature, label=self._name)
                self.sensor_temperature_target = None

        if self.is_active(GreatEyesActivities.CoolingDown) or self.is_active(GreatEyesActivities.WarmingUp):
            sensor_temp = self.get_sensor_temperature()

            # Read straight after the temperature call, which is a wrapper that does refresh
            # the status. Status 11 is 'cooling turned off', and it is the CAMERA saying it
            # has reset its own cooling control because the TEC backside is too hot -- see
            # retreat_to_room_temperature. Checking it matters because it can arrive long
            # before the backside poll above notices: that poll runs once per
            # temp.check_interval and compares against our own 55C, while this is the
            # camera's own threshold and its own decision.
            #
            # Caveat, and it is the MAST_spec#87 one: ge.Status is a module global shared by
            # all four band threads, so this can read another band's status. The cost of a
            # false positive is backing one camera off to room temperature and saying so in
            # the log, which is recoverable and loud; the cost of missing a real one is a hot
            # TEC. Worth it in that direction only.
            if ge.Status == COOLING_TURNED_OFF_STATUS and self.is_active(GreatEyesActivities.CoolingDown):
                self.retreat_to_room_temperature("camera reported status 11, cooling turned off")
                return

            if sensor_temp is None:
                if not dll_busy:
                    self.append_error("failed reading sensor temperature")
            else:
                switch_temp_control_off = False
                should_power_off = False
                if (
                    self.is_active(GreatEyesActivities.CoolingDown)
                    and abs(sensor_temp - self.settings.temp.target_cool) <= 1
                ):
                    self.end_activity(GreatEyesActivities.CoolingDown, label=self._name)
                    if self.is_active(GreatEyesActivities.StartingUp):
                        self.end_activity(GreatEyesActivities.StartingUp, label=self._name)

                    # Reaching the set point ends the COOLING DOWN activity, not the cooling.
                    # This used to set switch_temp_control_off here, and the SDK documents
                    # TemperatureControl_SwitchOff as "switch sensor cooling off completely" --
                    # so the sensor started drifting back up the moment it arrived, and nothing
                    # re-cools it: cool_down() is called only from startup().
                    #
                    # Measured 2026-09-07: all four bands reached 15C, the control was switched
                    # off, and they were at 20C two minutes later and 23C and still climbing
                    # four minutes after that. Three exposures were taken across that ramp.
                    # Dark current roughly doubles every 5-7C, so frames minutes apart sat at
                    # materially different dark levels and no dark taken at one point on the
                    # ramp matches a frame taken at another.
                    #
                    # The vendor uses SwitchOff for exactly one thing, and it is not this: the
                    # SDK header says to turn cooling off if the BACKSIDE temperature exceeds
                    # 55C and to contact greateyes. That check lives above and only logs; using
                    # the same call on the success path inverted the vendor's model.
                    #
                    # WarmingUp still switches off below, which is correct -- that path runs
                    # during shutdown, which is what SwitchOff is for.

                if self.is_active(GreatEyesActivities.WarmingUp) and abs(sensor_temp - self.settings.temp.target_warm) <= 1:
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
            self.temperature_read_failed("sensor")
            return None
        self.consecutive_no_camera_reads = 0
        return ret

    def temperature_read_failed(self, which: str) -> None:
        """Record a failed temperature read, and bring `detected` down if the camera is gone.

        Called with the SDK status still describing the read that just failed -- nothing may
        call into the SDK between that call and this.
        """
        # Captured once. sdk_status() reads the same globals and nothing between touches the
        # SDK, so the logged text and the decision cannot disagree.
        status = ge.Status
        self.append_error(f"failed to read {which} temperature (ret={FAILED_TEMPERATURE}, {sdk_status()})")

        if status != NO_CAMERA_STATUS:
            # A read that failed for some other reason says nothing about the session.
            self.consecutive_no_camera_reads = 0
            return

        self.consecutive_no_camera_reads += 1
        if self.consecutive_no_camera_reads < NO_CAMERA_READS_BEFORE_DEMOTING:
            return

        # Bringing `detected` down is the whole point: on_timer probes only when it is False,
        # and probe() -> try_connect_camera() is what re-establishes the session. Band G sat
        # unusable for hours on 2026-09-08 with the camera powered and its server accepting
        # on port 12345, purely because nothing ever set this and so nothing ever retried.
        self.warning(
            f"camera reports 'no camera detected' on {self.consecutive_no_camera_reads} consecutive "
            f"temperature reads: marking it undetected so the probe re-establishes the session"
        )
        self.consecutive_no_camera_reads = 0
        self._detected = False
        self._connected = False

    def get_back_temperature(self) -> float | None:
        if not self.detected:
            return None

        assert self.ge_device is not None
        if ge.DllIsBusy(addr=self.ge_device):
            return None

        ret = ge.TemperatureControl_GetTemperature(thermistor=1, addr=self.ge_device)
        if ret == FAILED_TEMPERATURE:
            self.temperature_read_failed("back")
            return None
        self.consecutive_no_camera_reads = 0
        return ret

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
            # This is a catch-all for any exceptions that might occur during the execution of the assignment.
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
            raise RuntimeError(
                f"cannot execute assignment because the following cameras are currently cooling down: "
                f"{', '.join(camera.name for camera in cooling_down)}"
            )

        thread = threading.Thread(
            name=f"deepspec-{self.band}-assignment",
            target=self.do_execute_assignment,
            args=[assignment, folder],
        )
        thread.start()
        return thread


class GreateyesFactory:
    _instances: ClassVar[dict[DeepspecBands, GreatEyes | None]] = {
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
    except Exception as e:  # noqa: BLE001 -- catch-all for logging, not recovery
        logger.error(f"{op}: could not build camera for band {band}: {e}")
        cameras[band] = None


cameras: dict[str, GreatEyes | None] = {}

for _band in list(get_args(DeepspecBands)):
    threading.Thread(name=f"make-deepspec-camera-{_band}", target=make_camera, args=[_band]).start()


if __name__ == "__main__":
    for c, value in cameras.items():
        print(f"{c}: {value}")
