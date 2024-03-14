import zaber_motion
import zaber_motion.ascii
import utils
from utils import Component, Activities, init_log
import logging
from enum import IntFlag, auto, Enum
from utils import PrettyJSONResponse
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
        self.position = position
        self.preset = preset


class Stage(Component, Activities):
    name: str
    axis: zaber_motion.ascii.Axis | None
    logger: logging.Logger
    target: float | None = None
    target_units: zaber_motion.Units | None
    presets: Dict[str, float]
    startup_position: float | None = None
    shutdown_position: float | None = None

    def __init__(self, name: str, ctlr=None):
        super().__init__()

        try:
            self.conf = Config().toml['stage'][name]
        except Exception as ex:
            logger.error(f"No stage named '{name}' in the config file")
            return

        self.name = name
        self.controller = ctlr
        self.logger = logging.getLogger(f"mast.spec.stage.{self.name}")
        init_log(self.logger)

        try:
            self.axis_id = int(self.conf['axis_id'])
        except ValueError:
            raise f"Bad or missing configuration item '[stages.{self.name}] -> axis_id"

        self.presets = dict()
        for key in self.conf['presets'].keys():
            self.presets[key] = self.conf['presets'][key]

        if 'startup' in self.conf:
            self.startup_position = float(self.conf['startup'])

        if 'shutdown' in self.conf:
            self.startup_position = float(self.conf['shutdown'])

        try:
            self.axis = self.controller.device.get_axis(self.axis_id)
            if self.axis.axis_type == zaber_motion.ascii.AxisType.UNKNOWN:
                self.logger.info(f"No stage name='{self.name}', axis_id={self.axis_id}")
                self.axis = None
                return
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

    def close_enough(self, microns: float) -> bool:
        """
        Checks if the current position is close enough to the supplied position
        :param microns:
        :return:
        """
        current = self.axis.get_position(unit=zaber_motion.Units.LENGTH_MICROMETRES)
        return abs(current - microns) <= 1

    def at_preset(self) -> str | None:
        if self.axis.is_busy():
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
        self.start_activity(StageActivities.ShuttingDown)
        if self.shutdown_position is not None:
            self.move_absolute(self.shutdown_position, unit=zaber_motion.Units.LENGTH_MICROMETRES)

    def startup(self):
        if self.axis is None:
            return
        self.start_activity(StageActivities.StartingUp)
        if self.axis.is_parked():
            self.axis.unpark()
        elif not self.axis.is_homed():
            self.start_activity(StageActivities.Homing)
            self.axis.home(wait_until_idle=False)

        if self.startup_position is not None:
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

    def status(self) -> StageStatus:
        return StageStatus(self.activities, self.position, self.at_preset())


class Controller(SwitchedPowerDevice, NetworkedDevice):

    def __init__(self):
        self.conf = Config().toml['stages']['controller']

        SwitchedPowerDevice.__init__(self, self.conf)
        NetworkedDevice.__init__(self, self.conf['network'])

        self.device: zaber_motion.ascii.Device = self.connect()

        self.stages: List = list()
        for name in Config().toml['stage']:
            self.stages.append(Stage(name=name, ctlr=self))

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


controller: Controller = Controller()

stages_dict = {}
for stage in controller.stages:
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
    for s in controller.stages:
        response[s.name] = {
            'device': f"{s.axis}",
            'axis_id': s.axis_id,
            'presets': s.presets,
            'startup': s.startup_position,
            'shutdown': s.shutdown_position,
        }
    return response


def stage_by_name(name: str) -> Stage | None:
    found = [s for s in controller.stages if s.name == name]
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
tag = 'stages'
router = APIRouter()

router.add_api_route(base_path, tags=[tag], endpoint=list_stages, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/position', tags=[tag], endpoint=get_position, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=get_status, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/move_absolute', tags=[tag], endpoint=move_absolute,
                     response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/move_relative', tags=[tag], endpoint=move_relative,
                     response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/move_to_preset', tags=[tag], endpoint=move_to_preset,
                     response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=startup, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=shutdown, response_class=PrettyJSONResponse)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=abort, response_class=PrettyJSONResponse)
