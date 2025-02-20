import zaber_motion
import zaber_motion.ascii
from common.utils import Component, BASE_SPEC_PATH, CanonicalResponse, function_name
from common.mast_logging import init_log
import logging
from enum import IntFlag, auto, Enum
from fastapi import APIRouter
from typing import List, get_args
from common.config import Config
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.networking import NetworkedDevice
from common.spec import StageLiteral, StageNames

logger = logging.getLogger('mast.spec.stage')
init_log(logger)


class StageActivities(IntFlag):
    Homing = auto()
    Moving = auto()
    StartingUp = auto()
    ShuttingDown = auto()
    Aborting = auto()


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

    def __init__(self,
                 name: str,
                 controller: zaber_motion.ascii.Device,
                 ):
        Component.__init__(self)

        self._was_shut_down = False
        try:
            self.conf = Config().get_specs()['stage'][name]
        except Exception as ex:
            logger.error(f"No stage named '{name}' in the config file")
            return

        self.name = name
        self.controller: zaber_motion.ascii.Device = controller
        self.peripheral = self.conf['peripheral']   # The Zaber model name of this stage
        self.logger = logging.getLogger(f"mast.spec.stage.{self.name}")
        init_log(self.logger)

        self._detected = False
        self.axis: zaber_motion.ascii.Axis | None = None
        for i in range(self.controller.axis_count):
            axis: zaber_motion.ascii.Axis = self.controller.get_axis(i+1) # axes are numbered from 1
            if axis.axis_type == zaber_motion.ascii.AxisType.UNKNOWN:
                continue
            if axis.peripheral_name == self.peripheral:
                self.axis = axis
                self._detected = True
                break

        self.presets = self.conf['presets']

        self.startup_position: float | None = None
        if 'startup' in self.conf:
            self.startup_position = float(self.conf['startup'])

        self.shutdown_position: float | None = None
        if 'shutdown' in self.conf:
            self.shutdown_position = float(self.conf['shutdown'])

        self.target: float | None = None
        self.target_units: zaber_motion.Units | None = None

        if self.detected:
            self.logger.info(f"found '{self.name}' stage, axis_number={self.axis.axis_number}, type={self.axis.axis_type}, "
                         f"peripheral='{self.peripheral}'")

            if self.axis.is_parked():
                self.axis.unpark()
            elif not self.axis.is_homed():
                self.start_activity(StageActivities.Homing)
                self.axis.home(wait_until_idle=False)


    @property
    def detected(self):
        return self._detected

    @property
    def connected(self) -> bool:
        return self.detected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    def name(self) -> str:
        return self.name

    def __repr__(self):
        return f"<Stage name={self.name}>"

    def close_enough(self, microns: float) -> bool:
        """
        Checks if the current position is close enough to the supplied position
        :param microns:
        :return:
        """
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self.name}' not detected"])

        current = self.axis.get_position(unit=zaber_motion.Units.LENGTH_MICROMETRES)
        return abs(current - microns) <= 1

    def at_preset(self, preset: StageLiteral) -> bool:
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self.name}' not detected"])

        if preset not in self.presets:
            return False
        return self.close_enough(self.presets[preset])

    def on_event(self, e: zaber_motion.ascii.AlertEvent):
        if e.status == 'IDLE':
            if self.is_active(StageActivities.Moving):
                self.end_activity(StageActivities.Moving)
                self.target = None
                self.target_units = None

            if self.is_active(StageActivities.ShuttingDown):
                if self.shutdown_position is not None:
                    if self.close_enough(self.shutdown_position):
                        self.end_activity(StageActivities.ShuttingDown)
                self.end_activity(StageActivities.ShuttingDown)

            if self.is_active(StageActivities.StartingUp):
                if self.startup_position is not None:
                    if self.close_enough(self.startup_position):
                        self.end_activity(StageActivities.StartingUp)
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Aborting):
                self.end_activity(StageActivities.Aborting)

            if self.is_active(StageActivities.Homing) and self.close_enough(0):
                self.end_activity(StageActivities.Homing)
        else:
            self.logger.error(f"Got unknown event: {e}")

    def move_relative(self, amount: float, unit: zaber_motion.Units):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self.name}' not detected"])
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_relative(amount, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    def move_absolute(self, position: float, unit: zaber_motion.Units):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self.name}' not detected"])
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_absolute(position, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    def move_to_preset(self, preset: StageLiteral):
        if not self.detected:
            return CanonicalResponse(errors=[f"stage '{self.name}' not detected"])
        if preset not in get_args(self.presets):
            raise ValueError(f"Bad preset '{preset}. Valid presets are; {",".join(self.presets.keys())}")

        self.target = self.presets[preset]
        self.target_units = zaber_motion.Units.LENGTH_MICROMETRES
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_absolute(self.target, self.target_units)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    @property
    def is_moving(self) -> bool:
        if not self.detected:
            return False
        return self.is_active(StageActivities.Moving)

    def shutdown(self):
        if not self.detected:
            return
        if self.shutdown_position and not self.close_enough(self.shutdown_position):
            self.start_activity(StageActivities.ShuttingDown)
            self.move_absolute(self.shutdown_position, unit=zaber_motion.Units.LENGTH_MICROMETRES)
        self._was_shut_down = True

    def startup(self):
        if not self.detected:
            return
        if self.axis.is_parked():
            self.axis.unpark()
        elif not self.axis.is_homed():
            self.start_activity(StageActivities.Homing)
            self.axis.home(wait_until_idle=False)

        if self.startup_position and not self.close_enough(self.startup_position):
            self.start_activity(StageActivities.StartingUp)
            self.move_absolute(self.startup_position, unit=zaber_motion.Units.LENGTH_MICROMETRES)
        self._was_shut_down = False

    def abort(self):
        if not self.detected:
            return
        self.start_activity(StageActivities.Aborting)
        self.axis.stop(wait_until_idle=False)

    def position(self, unit: zaber_motion.units.Units = zaber_motion.units.Units.NATIVE) -> float | None:
        if not self.detected:
            return float('nan')
        return self.axis.get_position(unit=unit)

    def status(self):
        ret = {
            'detected': self.detected,
            'presets': self.presets,
        }
        if self.detected:
            at_preset = [preset for preset in self.presets if self.at_preset(preset)]
            at_preset = at_preset[0] if at_preset else None

            ret |= {
                'activities': self.activities,
                'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
                'at_preset': at_preset,
                'position': self.position,
            }

        return ret

    @property
    def operational(self) -> bool:
        return self.detected

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = f"stage '{self.name}':"
        if not self.detected:
            ret.append(f"{label} not detected")
        return ret


class Controller(SwitchedOutlet, NetworkedDevice):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Controller, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        op = function_name()

        self.conf = Config().get_specs()['stage']['controller']
        self.stages: List = list()

        self.detected = False

        SwitchedOutlet.__init__(self, domain=OutletDomain.Spec, outlet_name='Stage')
        powered = False
        if self.power_switch.detected:
            if not self.is_on():
                self.power_on()
            if not self.is_on():
                logger.error(f"could not power ON outlet {self.power_switch}:{self.outlet_name}")
                powered = False
            else:
                powered = True
        else:
            logger.error(f"power switch {self.power_switch} not detected")
            powered = False

        NetworkedDevice.__init__(self, self.conf)

        self.device = None
        if powered:
            self.device: zaber_motion.ascii.Device | None = self.connect()
            if not self.device:
                logger.error(f"{op}: stage controller not detected")
            elif self.device.axis_count < 3:
                logger.error(f"{op}:stage controller has too few axes ({self.device.axis_count} instead of 3)")
            else:
                self.detected = True
                self.fiber_stage = Stage(name='fiber', controller=self.device)
                self.focusing_stage = Stage(name='focusing', controller=self.device)
                self.disperser_stage = Stage(name='disperser', controller=self.device)

                self.stages: List[Stage] = [self.fiber_stage, self.focusing_stage, self.disperser_stage]


    @staticmethod
    def on_error(arg):
        logger.error(f"on_error: {arg}")

    def on_completion(self, event: zaber_motion.ascii.AlertEvent):
        for st in self.stages:
            if st.detected and st.axis.axis_number == event.axis_number:
                st.on_event(event)
                return

    def on_next(self, event: zaber_motion.ascii.AlertEvent):
        for st in self.stages:
            if st.detected and st.axis.axis_number == event.axis_number:
                st.on_event(event)
                return

    def connect(self) -> zaber_motion.ascii.Device | None:
        ret: zaber_motion.ascii.Device

        try:
            conn = zaber_motion.ascii.Connection.open_tcp(host_name=self.network.ipaddr)
        except zaber_motion.ConnectionFailedException as ex:
            logger.error(f"cannot connect to stage controller at '{self.network.ipaddr}' (error: {ex})")
            self.detected = False
            return None

        devices = conn.detect_devices(identify_devices=True)
        if len(devices) < 1:
            raise Exception(f"no Zaber controllers")

        conn.enable_alerts()
        conn.alert.subscribe(on_error=self.on_error, on_completed=self.on_completion, on_next=self.on_next)
        ret = conn.get_device(1)
        ret.identify()

        return ret

zaber_controller: Controller | None = None
try:
    zaber_controller = Controller()
except zaber_motion.ConnectionFailedException as e:
    logger.error(f"cannot connect to Zaber controller (error: {e})")

units_dict = {}
reverse_units_dict = {}
for u in zaber_motion.Units:
    if u.name.startswith('LENGTH_') or u.name == 'NATIVE':
        v = u.name.replace('LENGTH_', '')
        units_dict[v] = v
        reverse_units_dict[v] = u
UnitNames = Enum('UnitNames', units_dict)


# FastApi stuff
def get_position(stage_name: StageNames, units: UnitNames) -> float:
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    return stage.position(unit=reverse_units_dict[units.value])


def get_status(stage_name: StageNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(value={'detected': False})
    return stage.status()


def move_absolute(stage_name: StageNames, position: float, units: UnitNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    stage.move_absolute(position, reverse_units_dict[units.value])


def move_relative(stage_name: StageNames, position: float, units: UnitNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    stage.move_relative(position, reverse_units_dict[units.value])


def move_to_preset(stage_name: StageNames, preset: StageLiteral):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    if preset not in stage.presets:
        return CanonicalResponse(errors=[f"bad preset '{preset}'. must be one of {stage.presets.keys()}"])

    stage.move_to_preset(preset.value)


def startup(stage_name: StageNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    stage.startup()


def shutdown(stage_name: StageNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    stage.shutdown()


def abort(stage_name: StageNames):
    stage = [s for s in zaber_controller.stages if s.name == stage_name][0]
    if not stage.detected:
        return CanonicalResponse(errors=[f"stage '{stage_name}' not detected"])
    stage.abort()

base_path = BASE_SPEC_PATH + 'stages'
tag = 'Stages'
router = APIRouter()

router.add_api_route(base_path + '/position', tags=[tag], endpoint=get_position)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=get_status)
router.add_api_route(base_path + '/move_absolute', tags=[tag], endpoint=move_absolute)
router.add_api_route(base_path + '/move_relative', tags=[tag], endpoint=move_relative)
router.add_api_route(base_path + '/move_to_preset', tags=[tag], endpoint=move_to_preset)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=abort)
