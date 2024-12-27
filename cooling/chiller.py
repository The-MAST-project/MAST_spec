from typing import List

from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.utils import Component, BASE_SPEC_PATH
from common.config import Config
from fastapi.routing import APIRouter


class Chiller(SwitchedOutlet, Component):

    def __init__(self):
        self.conf = Config().get_specs()['chiller']
        SwitchedOutlet.__init__(self, domain=OutletDomain.Spec, outlet_name='Chiller')

        if not self.power_switch.detected:
            return

        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

    def __repr__(self):
        return f"Chiller"

    def startup(self):
        if not self.is_on():
            self.power_on()

        self._was_shut_down = False

    def shutdown(self):
        if self.is_on():
            self.power_off()

        self._was_shut_down = True

    def abort(self):
        pass

    @property
    def detected(self):
        return self.is_on()

    @property
    def connected(self):
        return self.is_on()

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @property
    def name(self) -> str:
        return 'chiller'

    @property
    def operational(self) -> bool:
        return self.power_switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.power_switch.detected:
            ret.append(f"chiller: {self.power_switch} not detected")
        elif self.is_off():
            ret.append('chiller: {self.power_switch}:{self.outlet_name} is OFF')
        return ret

    def status(self):
        return {
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
        }

chiller = Chiller()

base_path = BASE_SPEC_PATH + 'chiller'
tag = 'Chiller'
router = APIRouter()

router.add_api_route(base_path + '/status', tags=[tag], endpoint=chiller.status)