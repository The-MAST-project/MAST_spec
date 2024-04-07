from utils import Component, BASE_SPEC_PATH, Component
from typing import List, Dict
from fastapi import APIRouter

from cameras.andor.newton import camera as highspec_camera, NewtonEMCCD
from cameras.greateyes.greateyes import DeepSpec, deepspec
from stage.stage import zaber_controller as stage_controller
from stage.stage import Stage
from filter_wheel.wheel import filter_wheeler, Wheel
from dlipower.dlipower.dlipower import PowerSwitch, PowerSwitchFactory


class Spec(Component):

    def __init__(self):
        Component.__init__(self)

        self.power_switches: List[PowerSwitch] = [
            PowerSwitchFactory.get_instance('1'),
            PowerSwitchFactory.get_instance('2')
        ]
        self.deepspec: DeepSpec = deepspec
        self.highspec_camera: NewtonEMCCD = highspec_camera
        self.stages: List[Stage] = stage_controller.stages
        self.wheels: List[Wheel] = filter_wheeler.wheels

        self.components: Dict[str, Component | List[Component]] = {
            'power_switches': self.power_switches,
            'deepspec': self.deepspec,
            'highspec': self.highspec_camera,
            'stages': self.stages,
            'wheels': self.wheels
        }

    def name(self) -> str:
        return 'spec'

    def status(self):
        return self.traverse_and_return('status')
    
    def startup(self):
        self.traverse_and_call('startup')
    
    def shutdown(self):
        self.traverse_and_call('shutdown')

    def abort(self):
        self.traverse_and_call('abort')

    def traverse_and_call(self, method_name: str):
        for key, component in self.components.items():
            if isinstance(component, list):
                for comp in component:
                    getattr(comp, method_name)()
            else:
                getattr(component, method_name)()

    def traverse_and_return(self, method_name: str) -> dict:
        ret = {}
        for key, component in self.components.items():
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

    def operational(self) -> bool:
        for x in self.components:
            if isinstance(x, list):
                for y in x:
                    if not y.operational:
                        return False
            elif isinstance(x, Component) and not x.operational:
                return False
        return True

    def why_not_operational(self) -> List[str]:
        ret = []
        for c in self.components:
            if isinstance(c, list):
                for x in c:
                    if isinstance(x, Component) and not x.operational:
                        for reason in x.why_not_operational:
                            ret.append(reason)
            elif isinstance(c, Component) and not c.operational:
                for reason in c.why_not_operational:
                    ret.append(reason)
        return ret


spec = Spec()


def startup():
    spec.startup()


def shutdown():
    spec.shutdown()


def status():
    return spec.status()


base_path = BASE_SPEC_PATH
tag = 'Spec'

router = APIRouter()
router.add_api_route(path=base_path + 'status', endpoint=status, tags=[tag])
router.add_api_route(path=base_path + 'startup', endpoint=startup, tags=[tag])
router.add_api_route(path=base_path + 'shutdown', endpoint=shutdown, tags=[tag])
