import zaber_motion
import zaber_motion.ascii
import utils
from utils import Component, init_log
import logging
from enum import IntFlag, auto, Enum
from fastapi import APIRouter
from typing import Dict, List
from config.config import Config
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from networking import NetworkedDevice

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

    def __init__(self, name: str, controller=None):
        Component.__init__(self)

        try:
            self.conf = Config().toml['stage'][name]
        except Exception as ex:
            logger.error(f"No stage named '{name}' in the config file")
            return

        self.name = name
        self.controller = controller
        self.logger = logging.getLogger(f"mast.spec.stage.{self.name}")
        init_log(self.logger)

        try:
            self.axis_id = int(self.conf['axis_id'])
        except ValueError:
            raise f"Bad or missing configuration item '[stages.{self.name}] -> axis_id"

        self.presets = dict()
        for key in self.conf['presets'].keys():
            self.presets[key] = self.conf['presets'][key]

        self.startup_position: float | None = None
        if 'startup' in self.conf:
            self.startup_position = float(self.conf['startup'])

        self.shutdown_position: float | None = None
        if 'shutdown' in self.conf:
            self.shutdown_position = float(self.conf['shutdown'])

        self.target: float | None = None
        self.target_units: zaber_motion.Units | None = None

        self.detected = False
        self.axis = None
        if self.controller and self.controller.detected:
            try:
                self.axis = self.controller.device.get_axis(self.axis_id)
                if self.axis.axis_type == zaber_motion.ascii.AxisType.UNKNOWN:
                    self.logger.info(f"No stage name='{self.name}', axis_id={self.axis_id}")
                    self.axis = None
                    return
                self.detected = True

                t = str(self.axis.axis_type).replace('AxisType.', '')
                self.logger.info(f"Found stage name='{self.name}', axis_id={self.axis.axis_number}, type={t}, "
                                 f"peripheral='{self.axis.identity.peripheral_name}'")

                if self.axis.is_parked():
                    self.axis.unpark()
                elif not self.axis.is_homed():
                    self.start_activity(StageActivities.Homing)
                    self.axis.home(wait_until_idle=False)

            except Exception as ex:
                self.logger.error(f"Exception: {ex}")

    def can_move(self):
        ret = []
        if not self.detected:
            ret.append('not detected')
        return ret

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
        current = self.axis.get_position(unit=zaber_motion.Units.LENGTH_MICROMETRES)
        return abs(current - microns) <= 1

    def at_preset(self) -> str | None:
        if not self.axis or self.axis.is_busy():
            return None
        for key, val in self.presets.items():
            if self.close_enough(val):
                return key
        return None

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
        if self.axis is None:
            return
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_relative(amount, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    def move_absolute(self, position: float, unit: zaber_motion.Units):
        if self.axis is None:
            return
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_absolute(position, unit=unit)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    def move_to_preset(self, preset: str):
        if self.axis is None:
            return
        if preset not in self.presets:
            raise ValueError(f"Bad preset '{preset}. Valid presets are; {",".join(self.presets.keys())}")

        self.target = self.presets[preset]
        self.target_units = zaber_motion.Units.LENGTH_MICROMETRES
        self.start_activity(StageActivities.Moving)
        try:
            self.axis.move_absolute(self.target, self.target_units)
        except zaber_motion.MotionLibException as ex:
            self.end_activity(StageActivities.Moving)
            self.logger.error(f"Exception {ex}")

    def shutdown(self):
        if self.axis is None:
            return
        if self.shutdown_position and not self.close_enough(self.shutdown_position):
            self.start_activity(StageActivities.ShuttingDown)
            self.move_absolute(self.shutdown_position, unit=zaber_motion.Units.LENGTH_MICROMETRES)

    def startup(self):
        if self.axis is None:
            return
        if self.axis.is_parked():
            self.axis.unpark()
        elif not self.axis.is_homed():
            self.start_activity(StageActivities.Homing)
            self.axis.home(wait_until_idle=False)

        if self.startup_position and not self.close_enough(self.startup_position):
            self.start_activity(StageActivities.StartingUp)
            self.move_absolute(self.startup_position, unit=zaber_motion.Units.LENGTH_MICROMETRES)

    def abort(self):
        if self.axis is None:
            return
        self.start_activity(StageActivities.Aborting)
        self.axis.stop(wait_until_idle=False)

    @property
    def position(self) -> float | None:
        if self.axis is None:
            return float('nan')
        return self.axis.get_position()

    def status(self):
        ret = {
            'detected': self.detected,
            'presets': self.presets,
        }
        if self.detected:
            ret['activities'] = self.activities
            ret['activities_verbal'] = self.activities.__repr__()
            ret['at_preset'] = self.at_preset()
            ret['position'] = self.position

        return ret

    @property
    def operational(self) -> bool:
        return self.detected

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        label = f"stage '{self.name}'"
        if not self.detected:
            ret.append(f"{label} not detected")
        return ret


class Controller(SwitchedPowerDevice, NetworkedDevice):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Controller, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        self.conf = Config().toml['stages']['controller']
        self.stages: List = list()

        self.detected = False
        self.power = SwitchedPowerDevice(self.conf)
        if self.power.switch.detected:
            if self.power.switch.is_off(self.power.outlet):
                self.power.switch.on(self.power.outlet)

        NetworkedDevice.__init__(self, self.conf)

        self.device = None
        if self.power.switch.detected:
            self.device: zaber_motion.ascii.Device = self.connect()
        if self.device:
            self.detected = True

        for name in Config().toml['stage']:
            self.stages.append(Stage(name=name, controller=self))

    @staticmethod
    def on_error(arg):
        logger.error(f"on_error: {arg}")

    def on_completion(self, e: zaber_motion.ascii.AlertEvent):
        for st in self.stages:
            if st.axis_id == e.axis_number:
                st.on_event(e)
                return

    def on_next(self, e: zaber_motion.ascii.AlertEvent):
        for st in self.stages:
            if st.axis_id == e.axis_number:
                st.on_event(e)
                return

    def connect(self) -> zaber_motion.ascii.Device:
        dev: zaber_motion.ascii.Device

        conn = zaber_motion.ascii.Connection.open_tcp(host_name=self.destination.address)
        devices = conn.detect_devices(identify_devices=True)
        if len(devices) < 1:
            raise f"No Zaber devices (controllers)"

        conn.enable_alerts()
        conn.alert.subscribe(on_error=self.on_error, on_completed=self.on_completion, on_next=self.on_next)
        dev = conn.get_device(1)
        dev.identify()

        return dev


zaber_controller: Controller = Controller()

stages_dict = {}
for stage in zaber_controller.stages:
    stages_dict[stage.name] = stage.name

StageNames = Enum('StageNames', stages_dict)

units_dict = {}
reverse_units_dict = {}
for u in zaber_motion.Units:
    if u.name.startswith('LENGTH_') or u.name == 'NATIVE':
        v = u.name.replace('LENGTH_', '')
        units_dict[v] = v
        reverse_units_dict[v] = u
UnitNames = Enum('UnitNames', units_dict)


# FastApi stuff
def list_stages():
    response = {}
    for s in zaber_controller.stages:
        response[s.name] = {
            'device': f"{s.axis}",
            'axis_id': s.axis_id,
            'presets': s.presets,
            'startup': s.startup_position,
            'shutdown': s.shutdown_position,
        }
    return response


def stage_by_name(name: str) -> Stage | None:
    found = [s for s in zaber_controller.stages if s.name == name]
    if len(found) == 1:
        return found[0]
    else:
        return None


def get_position(stage_name: StageNames):
    st = stage_by_name(stage_name.value)
    if st:
        return st.position
    else:
        return {
            'Error': f"No physical stage for '{stage_name.value}'"
        }


def get_status(stage_name: StageNames):
    st = stage_by_name(stage_name.value)
    if st:
        return st.status()
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


def move_absolute(stage_name: StageNames, position: float, units: UnitNames):
    st = stage_by_name(stage_name.value)
    if st:
        st.move_absolute(position, reverse_units_dict[units.value])
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


def move_relative(stage_name: StageNames, position: float, units: UnitNames):
    st = stage_by_name(stage_name.value)
    if st:
        st.move_relative(position, reverse_units_dict[units.value])
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


PresetNames = Enum('PresetNames', {
    'Ca': 'Ca',
    'Mg': 'Mg',
    'Halpha': 'Halpha',
    'DeepSpec': 'DeepSpec',
    'HighSpec': 'HighSpec'
})


def move_to_preset(stage_name: StageNames, preset: PresetNames):
    st = stage_by_name(stage_name.value)
    if st and st.axis:
        if (preset.value == 'HighSpec' or preset.value == 'DeepSpec') and st.name != 'fiber':
            return {
                'Error': f"Only the 'fiber' stage has presets named 'DeepSpec' or 'HighSpec'"
            }
        if (preset.value == 'Ca' or preset.value == 'Mg' or preset.value == 'Halpha') and st.name == 'fiber':
            return {
                'Error': f"The 'fiber' stage has presets named 'DeepSpec' or 'HighSpec'"
            }

        st.move_to_preset(preset.value)
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


def startup(stage_name: StageNames):
    st = stage_by_name(stage_name.value)
    if st.axis is not None:
        st.startup()
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


def shutdown(stage_name: StageNames):
    st = stage_by_name(stage_name.value)
    if st.axis is not None:
        st.shutdown()
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


def abort(stage_name: StageNames):
    st = stage_by_name(stage_name.value)
    if st.axis is not None:
        st.abort()
    else:
        return {
            'Error': f"No physical stage for '{stage_name}'"
        }


base_path = utils.BASE_SPEC_PATH + 'stages'
tag = 'Stages'
router = APIRouter()

router.add_api_route(base_path, tags=[tag], endpoint=list_stages)
router.add_api_route(base_path + '/position', tags=[tag], endpoint=get_position)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=get_status)
router.add_api_route(base_path + '/move_absolute', tags=[tag], endpoint=move_absolute)
router.add_api_route(base_path + '/move_relative', tags=[tag], endpoint=move_relative)
router.add_api_route(base_path + '/move_to_preset', tags=[tag], endpoint=move_to_preset)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=abort)
