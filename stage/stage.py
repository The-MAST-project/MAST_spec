from __future__ import annotations

import logging
from enum import Enum, IntFlag
from typing import TYPE_CHECKING, List

import zaber_motion
import zaber_motion.ascii
import zaber_motion.exceptions
from fastapi import APIRouter

from common.activities import StageActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.statuses import SpecStageStatus
from common.networking import NetworkedDevice
from common.spec import GratingNames, SpecNames, SpecStageNames
from common.utils import caller_name, function_name

logger = logging.getLogger("mast.spec.stage")
init_log(logger)

if TYPE_CHECKING:
    from spec import Spec


units_dict = {}
reverse_units_dict = {}
for u in zaber_motion.Units:
    if u.name.startswith("LENGTH_") or u.name == "NATIVE":
        v = u.name.replace("LENGTH_", "")
        units_dict[v] = v
        reverse_units_dict[v] = u
UnitNames = Enum("UnitNames", units_dict)


class StageStatus:
    activities: IntFlag
    position: float
    preset: str | None

    def __init__(self, activities: IntFlag, position: float, preset: str | None):
        self.activities = activities
        self.activities_verbal = activities.__repr__()
        self.position = position
        self.preset = preset


class Stage(Component):
    def __init__(
        self,
        name: str,
        controller: zaber_motion.ascii.Device,
    ):
        Component.__init__(self, StageActivities)

        self._was_shut_down = False
        specs_conf = Config().get_specs()
        if name == "fiber":
            self.conf = specs_conf.stage.fiber
        elif name == "disperser":
            self.conf = specs_conf.stage.disperser
        elif name == "focusing":
            self.conf = specs_conf.stage.focusing

        self.max_position = self.conf.max_position

        self._name = f"{name}"
        self.controller: zaber_motion.ascii.Device = controller
        self.peripheral = self.conf.peripheral  # The Zaber model name of this stage
        self.logger = logging.getLogger(f"mast.spec.stage.{self._name}")
        init_log(self.logger)

        self._detected = False
        self.axis: zaber_motion.ascii.Axis | None = None
        for i in range(1, self.controller.axis_count + 1):
            axis: zaber_motion.ascii.Axis = self.controller.get_axis(
                i
            )  # axes are numbered from 1
            try:
                axis.activate()
                self.controller.identify()  # MUST be called after axis.activate() to forget previous stage and identify current
            except Exception as ex:
                if (
                    "The command failed to execute because the axis is inactive"
                    in f"{ex}"
                ):
                    self._detected = False
                    continue

                else:
                    logger.error(f"Stage.__init__: {ex=}")
                    continue

            if axis.axis_type == zaber_motion.ascii.AxisType.UNKNOWN:
                continue
            if axis.peripheral_name == self.peripheral:
                self.axis = axis
                self._detected = True
                break

        self.presets = self.conf.presets

        self.startup_preset = self.conf.startup_preset
        self.shutdown_preset = self.conf.shutdown_preset

        self.target: float | None = None
        self.target_units: zaber_motion.Units | None = None

        if self.detected:
            assert self.axis is not None
            self.logger.info(
                f"found '{self._name}' stage, axis_number={self.axis.axis_number}, type={self.axis.axis_type}, "
                f"peripheral='{self.peripheral}', range=0..{self.max_position}"
            )

            if self.axis.is_parked():
                self.axis.unpark()

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def full_name(self):
        return f"{self._name}-stage"

    @property
    def detected(self):
        return self._detected

    @detected.setter
    def detected(self, value):
        self._detected = value

    @property
    def connected(self) -> bool:
        return self.detected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    def __repr__(self):
        return f"<Stage name={self._name}>"

    def close_enough(self, microns: float) -> bool:
        """
        Checks if the current position is close enough to the supplied position
        :param microns:
        :return:
        """
        if not self.detected:
            return False

        assert self.axis is not None
        current = self.axis.get_position(unit=zaber_motion.Units.NATIVE)
        return abs(current - microns) <= 1

    @property
    def at_preset(self) -> str | None:
        if not self.detected:
            return None

        for preset, position in self.presets.items():
            if self.close_enough(position):
                return preset

        return None

    def on_event(self, e: zaber_motion.ascii.AlertEvent):
        if e is None:
            logger.warning(f"{self.name}: ignoring None event")
            return

        if e.status == "IDLE":
            if self.is_active(StageActivities.Moving):
                self.end_activity(StageActivities.Moving, label=f"{self.full_name}: ")
                self.target = None
                self.target_units = None

            if self.is_active(StageActivities.ShuttingDown):
                if self.shutdown_preset is not None:
                    if self.close_enough(self.presets[self.shutdown_preset]):
                        self.end_activity(
                            StageActivities.ShuttingDown, label=f"{self.full_name}: "
                        )
                self.end_activity(
                    StageActivities.ShuttingDown, label=f"{self.full_name}: "
                )

            if self.is_active(StageActivities.StartingUp):
                if self.startup_preset is not None:
                    if self.close_enough(self.presets[self.startup_preset]):
                        self.end_activity(
                            StageActivities.StartingUp, label=f"{self.full_name}: "
                        )
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Aborting):
                self.end_activity(StageActivities.Aborting, label=f"{self.full_name}: ")

            if self.is_active(StageActivities.Homing) and self.close_enough(0):
                self.end_activity(StageActivities.Homing, label=f"{self.full_name}: ")
        else:
            self.logger.error(f"Got unknown event: {e}")

    def move_relative(
        self,
        amount: float,
        unit: zaber_motion.Units = zaber_motion.Units.NATIVE,
    ):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self._name}' not detected"])
        self.start_activity(
            StageActivities.Moving,
            label=f"{self.full_name}: ",
            details=[f"relative to {self.position(unit=unit):.5f} by {amount} {unit}"],
        )

        current_position = self.position(unit=unit)
        if current_position is None:
            self.logger.error(
                f"{function_name()}: cannot get current position for relative move"
            )
            return

        # if self._out_of_range(current_position + amount, unit):
        #     raise ValueError(
        #         f"{function_name()}: relative move from {current_position=} by {amount=} {unit=} out of range [0..{self.max_position}]"
        #     )

        assert self.axis is not None
        try:
            self.axis.move_relative(amount, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving, label=f"{self.full_name}: ")
            self.logger.error(f"{function_name()}: Exception {ex}")

    def _out_of_range(self, position: float, unit: zaber_motion.Units) -> bool:
        if unit != zaber_motion.Units.NATIVE:
            raise ValueError(
                f"{caller_name()}: only NATIVE units are supported (got {unit})"
            )

        if self.max_position is None:
            return False
        return position < 0 or position > self.max_position

    def move_absolute(
        self,
        position: float,
        unit: zaber_motion.Units = zaber_motion.Units.NATIVE,
    ):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self._name}' not detected"])

        # if self._out_of_range(position, unit):
        #     raise ValueError(
        #         f"{function_name()}: position {position} out of range (0-{self.max_position})"
        #     )

        self.start_activity(
            StageActivities.Moving,
            label=f"{self.full_name}: ",
            details=[f"absolute {position=:.5f} {unit}"],
        )

        assert self.axis is not None
        try:
            self.axis.move_absolute(position, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving, label=f"{self.full_name}: ")
            self.logger.error(f"{function_name()}: Exception {ex}")

    def move_to_preset(self, preset: str):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self._name}' not detected"])
        if preset not in self.presets:
            raise ValueError(
                f"Bad preset '{preset}'. Valid presets are: {','.join(self.presets.keys())}"
            )

        self.target = self.presets[preset]
        self.target_units = zaber_motion.Units.NATIVE
        self.start_activity(
            StageActivities.Moving,
            label=f"{self.full_name}: ",
            details=[f"to '{preset=}' at {self.target:.5f} {self.target_units}"],
        )

        assert self.axis is not None and self.target is not None
        try:
            self.axis.move_absolute(self.target, self.target_units)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving, label=f"{self.full_name}: ")
            self.logger.error(f"{function_name()}: Exception {ex}")

    @property
    def is_moving(self) -> bool:
        if not self.detected:
            return False
        return self.is_active(StageActivities.Moving)

    def shutdown(self):
        if not self.detected:
            return
        if self.shutdown_preset and not self.close_enough(
            self.presets[self.shutdown_preset]
        ):
            self.start_activity(
                StageActivities.ShuttingDown, label=f"{self.full_name}: "
            )
            self.move_absolute(
                self.presets[self.shutdown_preset],
                unit=zaber_motion.Units.NATIVE,
            )
        self._was_shut_down = True

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(StageActivities.ShuttingDown)

    def powerdown(self):
        pass

    def startup(self):
        if not self.detected:
            return

        assert self.axis is not None
        parked = False
        try:
            parked = self.axis.is_parked()
        except zaber_motion.exceptions.ConnectionClosedException:
            self.detected = False
            return

        if parked:
            self.axis.unpark()
        elif not self.axis.is_homed():
            self.start_activity(StageActivities.Homing, label=f"{self.name}: ")
            self.axis.home(wait_until_idle=False)

        if self.startup_preset and not self.close_enough(
            self.presets[self.startup_preset]
        ):
            self.start_activity(StageActivities.StartingUp, label=f"{self.full_name}: ")
            self.move_absolute(
                self.presets[self.startup_preset],
                unit=zaber_motion.Units.NATIVE,
            )
        self._was_shut_down = False

    def abort(self):
        if not self.detected:
            return

        assert self.axis is not None
        self.start_activity(StageActivities.Aborting, label=f"{self.full_name}: ")
        self.axis.stop(wait_until_idle=False)

    def position(
        self, unit: zaber_motion.units.Units = zaber_motion.units.Units.NATIVE
    ) -> float | None:
        if not self.detected:
            return float("nan")

        assert self.axis is not None
        return self.axis.get_position(unit=unit)

    def status(self) -> SpecStageStatus:
        import math

        pos = self.position(unit=zaber_motion.Units.NATIVE)
        if pos is not None:
            if math.isnan(pos):
                pos = None
            else:
                pos = round(pos)

        ret = SpecStageStatus(
            detected=self.detected,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
            presets=self.presets,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            position=int(pos) if pos is not None else None,
            position_nm=self.position(unit=zaber_motion.Units.LENGTH_NANOMETRES),
            position_um=self.position(unit=zaber_motion.Units.LENGTH_MICROMETRES),
            position_mm=self.position(unit=zaber_motion.Units.LENGTH_MILLIMETRES),
            position_cm=self.position(unit=zaber_motion.Units.LENGTH_CENTIMETRES),
            at_preset=self.at_preset,
        )

        return ret

    @property
    def operational(self) -> bool:
        return self.detected

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = f"stage '{self._name}':"
        if not self.detected:
            ret.append(f"{label} not detected")
        return ret


class StageController(SwitchedOutlet, NetworkedDevice):
    _instance = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(StageController, cls).__new__(cls)
        return cls._instance

    def __init__(self, spec: Spec | None = None):
        if self._initialized:
            return
        op = function_name()

        self.conf = Config().get_specs().stage.controller
        self.stages = []
        self.spec = spec

        self.detected = False

        SwitchedOutlet.__init__(
            self, domain=OutletDomain.SpecOutlets, outlet_name="Stage"
        )
        powered = False

        assert self.power_switch is not None
        if self.power_switch.detected:
            if not self.is_on():
                self.power_on()
            if not self.is_on():
                logger.error(
                    f"could not power ON outlet {self.power_switch}:{self.outlet_names[0]}"
                )
                powered = False
            else:
                powered = True
        else:
            logger.error(f"power switch {self.power_switch} not detected")
            powered = False

        NetworkedDevice.__init__(self, self.conf.model_dump())

        self.device = None
        self.fiber_stage = self.disperser_stage = self.focusing_stage = None

        if powered:
            self.device: zaber_motion.ascii.Device | None = self.connect()
            if not self.device:
                logger.error(f"{op}: stage controller not detected")
            elif self.device.axis_count < 3:
                logger.error(
                    f"{op}:stage controller has too few axes ({self.device.axis_count} instead of 3)"
                )
            else:
                self.detected = True
                try:
                    self.fiber_stage = Stage(name="fiber", controller=self.device)
                except:  # noqa: E722
                    self.fiber_stage = None

                try:
                    self.focusing_stage = Stage(name="focusing", controller=self.device)
                except:  # noqa: E722
                    self.focusing_stage = None

                try:
                    self.disperser_stage = Stage(
                        name="disperser", controller=self.device
                    )
                except:  # noqa: E722
                    self.disperser_stage = None

                self.stages: List[Stage | None] = [
                    self.fiber_stage,
                    self.focusing_stage,
                    self.disperser_stage,
                ]
        self._initialized = True

    @staticmethod
    def on_error(arg):
        logger.error(f"on_error: {arg}")

    def on_completion(self, event: zaber_motion.ascii.AlertEvent | None = None):
        if event is None:
            logger.warning("on_completion: ignoring None event")
            return

        for st in self.stages:
            if (
                st is not None
                and st.detected
                and st.axis is not None
                and st.axis.axis_number == event.axis_number
            ):
                st.on_event(event)
                return

    def on_next(self, event: zaber_motion.ascii.AlertEvent):
        if event is None:
            logger.warning("on_next: ignoring None event")
            return

        for st in self.stages:
            if (
                st is not None
                and st.detected
                and st.axis is not None
                and st.axis.axis_number == event.axis_number
            ):
                st.on_event(event)
                return

    def connect(self) -> zaber_motion.ascii.Device | None:
        ret: zaber_motion.ascii.Device

        try:
            conn = zaber_motion.ascii.Connection.open_tcp(host_name=self.network.ipaddr)
        except zaber_motion.ConnectionFailedException as ex:
            logger.error(
                f"cannot connect to stage controller at '{self.network.ipaddr}' (error: {ex})"
            )
            self.detected = False
            return None

        devices_database_file = "C:/MAST/Downloads/devices-public-v2.sqlite"
        zaber_motion.Library.set_device_db_source(
            zaber_motion.DeviceDbSourceType.FILE,
            devices_database_file,
        )
        logger.info(f"using local ZABER device database: '{devices_database_file}'")

        try:
            devices = conn.detect_devices(identify_devices=True)
        except Exception as ex:
            logger.error(f"cannot detect Zaber devices: {ex}")
            self.detected = False
            return None

        if len(devices) < 1:
            raise Exception("no Zaber controllers")

        conn.enable_alerts()
        conn.alert.subscribe(
            on_error=self.on_error,
            on_completed=self.on_completion,
            on_next=self.on_next,
        )
        ret = conn.get_device(1)
        identity = ret.identify()
        logger.info(f"ZABER Controller #1: {identity}")

        return ret

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self.stages if s is not None]

    def find_stage(self, stage_name: str) -> Stage | CanonicalResponse:
        """
        Tries to find a stage by name.  Returns either the stage (if the name matches and was detected)
        or a canonical response with errors.
        """
        if stage_name not in self.stage_names:
            return CanonicalResponse(
                errors=[f"unknown '{stage_name=}' (known names: {self.stage_names})"]
            )

        stage = [s for s in self.stages if s is not None and s.name == stage_name][0]
        if not stage.detected:
            return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])

        return stage

    # FastApi stuff
    def endpoint_get_stage_position(
        self, stage_name: SpecStageNames, units: UnitNames
    ) -> CanonicalResponse:
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        return CanonicalResponse(
            value=stage.position(unit=reverse_units_dict[units.value])
        )

    def endpoint_get_stage_status(self, stage_name: SpecStageNames):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        return CanonicalResponse(value=stage.status())

    def endpoint_stage_move_absolute(
        self, stage_name: SpecStageNames, position: float, units: UnitNames
    ):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        stage.move_absolute(position, reverse_units_dict[units.value])

        return CanonicalResponse_Ok

    def endpoint_stage_move_relative(
        self, stage_name: SpecStageNames, amount: float, units: UnitNames
    ):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        stage.move_relative(amount, reverse_units_dict[units.value])

        return CanonicalResponse_Ok

    def endpoint_move_fiber_to_preset(
        self, preset_name: SpecNames
    ) -> CanonicalResponse:
        if self.fiber_stage is None:
            return CanonicalResponse(errors=["self.fiber_stage is None"])
        self.fiber_stage.move_to_preset(preset=preset_name)
        return CanonicalResponse_Ok

    def endpoint_move_disperser_to_preset(
        self, preset_name: GratingNames
    ) -> CanonicalResponse:
        if self.disperser_stage is None:
            return CanonicalResponse(errors=["self.disperser_stage is None"])
        self.disperser_stage.move_to_preset(preset=preset_name)
        return CanonicalResponse_Ok

    def endpoint_move_focusing_to_preset(
        self, preset_name: GratingNames
    ) -> CanonicalResponse:
        if self.focusing_stage is None:
            return CanonicalResponse(errors=["self.focusing_stage is None"])
        self.focusing_stage.move_to_preset(preset=preset_name)
        return CanonicalResponse_Ok

    def endpoint_stage_startup(self, stage_name: SpecStageNames):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        stage.startup()

        return CanonicalResponse_Ok

    def endpoint_stage_shutdown(self, stage_name: SpecStageNames):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        stage.shutdown()

        return CanonicalResponse_Ok

    def endpoint_stage_abort(self, stage_name: SpecStageNames):
        ret = self.find_stage(stage_name)
        if isinstance(ret, CanonicalResponse):
            return ret

        stage = ret
        stage.abort()

        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + "/stages"
        tag = "Stages"
        router = APIRouter()

        router.add_api_route(
            base_path + "/position",
            tags=[tag],
            endpoint=self.endpoint_get_stage_position,
        )
        router.add_api_route(
            base_path + "/status", tags=[tag], endpoint=self.endpoint_get_stage_status
        )
        router.add_api_route(
            base_path + "/move_absolute",
            tags=[tag],
            endpoint=self.endpoint_stage_move_absolute,
        )
        router.add_api_route(
            base_path + "/move_relative",
            tags=[tag],
            endpoint=self.endpoint_stage_move_relative,
        )
        router.add_api_route(
            base_path + "/move_fiber_to_preset",
            tags=[tag],
            endpoint=self.endpoint_move_fiber_to_preset,
        )
        router.add_api_route(
            base_path + "/move_disperser_to_preset",
            tags=[tag],
            endpoint=self.endpoint_move_disperser_to_preset,
        )
        router.add_api_route(
            base_path + "/move_focusing_to_preset",
            tags=[tag],
            endpoint=self.endpoint_move_focusing_to_preset,
        )
        router.add_api_route(
            base_path + "/startup", tags=[tag], endpoint=self.endpoint_stage_startup
        )
        router.add_api_route(
            base_path + "/shutdown", tags=[tag], endpoint=self.endpoint_stage_shutdown
        )
        router.add_api_route(
            base_path + "/abort", tags=[tag], endpoint=self.endpoint_stage_abort
        )

        return router


if __name__ == "__main__":
    try:
        zaber_controller = StageController()
    except zaber_motion.ConnectionFailedException as e:
        logger.error(f"cannot connect to Zaber controller (error: {e})")
        zaber_controller = None
