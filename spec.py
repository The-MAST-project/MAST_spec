import itertools
import threading

from utils import BASE_SPEC_PATH, Component, init_log, PathMaker
from typing import List, Dict
from fastapi import APIRouter
from enum import IntFlag, auto
import time
import logging
from itertools import chain

from cameras.andor.newton import camera as highspec_camera, NewtonEMCCD, NewtonActivities
from cameras.greateyes.greateyes import DeepSpec, deepspec, GreatEyesActivities
from stage.stage import zaber_controller as stage_controller, Stage, StageActivities
from filter_wheel.wheel import filter_wheeler, Wheel
from dlipower.dlipower.dlipower import PowerSwitch, PowerSwitchFactory


class SpecActivities(IntFlag):
    Acquiring = auto()


class Spec(Component):

    def __init__(self):
        Component.__init__(self)
        self.logger = logging.Logger('spec')
        init_log(self.logger)

        self.power_switches: List[PowerSwitch] = [
            PowerSwitchFactory.get_instance('1'),
            PowerSwitchFactory.get_instance('2')
        ]
        self.deepspec: DeepSpec = deepspec
        self.highspec_camera: NewtonEMCCD = highspec_camera
        self.stages: List[Stage] = stage_controller.stages
        self.wheels: List[Wheel] = filter_wheeler.wheels

        self.components_dict: Dict[str, Component | List[Component]] = {
            'power_switches': self.power_switches,
            'deepspec': self.deepspec,
            'highspec': self.highspec_camera,
            'stages': self.stages,
            'wheels': self.wheels
        }
        self.components = []
        for comp in self.power_switches:
            self.components.append(comp)
        self.components.append(highspec_camera)
        for comp in self.deepspec.cameras:
            self.components.append(comp)
        for comp in self.stages:
            self.components.append(comp)
        for comp in self.wheels:
            self.components.append(comp)

        self.highspec_exposure_seconds = 15
        self.deepspec_exposure_seconds = 10

    def name(self) -> str:
        return 'spec'

    def status(self):
        ret = self.traverse_and_return('status')
        ret['activities'] = self.activities
        ret['activities_verbal'] = self.activities.__repr__()
        ret['operational'] = self.operational
        ret['why_not_operational'] = self.why_not_operational
        return ret
    
    def startup(self):
        self.traverse_and_call('startup')
    
    def shutdown(self):
        self.traverse_and_call('shutdown')

    def abort(self):
        self.traverse_and_call('abort')

    def traverse_and_call(self, method_name: str):
        for key, component in self.components_dict.items():
            if isinstance(component, list):
                for comp in component:
                    getattr(comp, method_name)()
            else:
                getattr(component, method_name)()

    def traverse_and_return(self, method_name: str) -> dict:
        ret = {}
        for key, component in self.components_dict.items():
            if isinstance(component, list):
                ret[key] = {}
                name = ''
                for comp in component:
                    if isinstance(comp.name, str):
                        name = comp.name
                    elif callable(comp.name):
                        name = comp.name()
                    ret[key][name] = getattr(comp, method_name)()
            else:
                ret[key] = getattr(component, method_name)()
        return ret

    @property
    def operational(self) -> bool:
        return all(map(lambda component: component.operational, self.components))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        for comp in self.components:
            for reason in comp.why_not_operational:
                ret.append(reason)
        return ret

    def do_acquire(self):
        found = [s for s in self.stages if s.name == 'fiber']
        stage = found[0]

        self.start_activity(SpecActivities.Acquiring)
        stage.move_to_preset(stage.presets['HighSpec'])
        while stage.is_active(StageActivities.Moving):
            time.sleep(1)

        path_maker = PathMaker()
        acquisition_folder = path_maker.make_acquisition_folder_name()
        self.highspec_camera.expose(self.highspec_exposure_seconds, acquisition=acquisition_folder)

        while True:
            time.sleep(2)
            if (self.highspec_camera.is_active(NewtonActivities.Exposing) or
                    self.highspec_camera.is_active(NewtonActivities.ReadingOut)):
                continue
            for cam in self.deepspec.cameras:
                if cam.is_active(GreatEyesActivities.Exposing):
                    continue
            break

        stage.move_to_preset(stage.presets['DeepSpec'])
        while stage.is_active(StageActivities.Moving):
            time.sleep(1)

        for cam in self.deepspec.cameras:
            cam.expose(self.deepspec_exposure_seconds, acquisition=acquisition_folder)

        while True:
            time.sleep(2)
            for cam in self.deepspec.cameras:
                if cam.is_active(GreatEyesActivities.Exposing):
                    continue
            break

        self.end_activity(SpecActivities.Acquiring)

    def acquire(self):
        stage: Stage | None = None
        for s in self.stages:
            if s.name == 'fiber':
                stage = s

        errors = []
        reasons = self.highspec_camera.can_expose()
        for reason in reasons:
            errors.append(f'highspec: {reason}')
        for cam in self.deepspec.cameras:
            reasons = cam.can_expose()
            for reason in reasons:
                errors.append(f'deepspec: {reason}')
        reasons = stage.can_move()
        for reason in reasons:
            errors.append(f'fiber-stage: {reason}')

        if len(errors) != 0:
            return {'error': errors}

        threading.Thread(name='spec-acquisition', target=self.do_acquire).start()

    def set_params(self, highspec_seconds: float, deepspec_seconds: float):
        self.highspec_exposure_seconds = highspec_seconds
        self.deepspec_exposure_seconds = deepspec_seconds


spec = Spec()


def startup():
    spec.startup()


def shutdown():
    spec.shutdown()


def acquire():
    spec.acquire()


def status():
    return spec.status()


def set_params(highspec_exposure: float, deepspec_exposure: float):
    spec.set_params(highspec_exposure, deepspec_exposure)


base_path = BASE_SPEC_PATH
tag = 'Spec'

router = APIRouter()
router.add_api_route(path=base_path + 'status', endpoint=status, tags=[tag])
router.add_api_route(path=base_path + 'startup', endpoint=startup, tags=[tag])
router.add_api_route(path=base_path + 'shutdown', endpoint=shutdown, tags=[tag])
router.add_api_route(path=base_path + 'setparams', endpoint=set_params, tags=[tag])
router.add_api_route(path=base_path + 'shutdown', endpoint=acquire, tags=[tag])
