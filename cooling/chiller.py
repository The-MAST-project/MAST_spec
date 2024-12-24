from typing import List

from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.utils import Component
from common.config import Config


class Chiller(SwitchedOutlet, Component):

    def __init__(self):
        self.conf = Config().get_specs()['chiller']
        SwitchedOutlet.__init__(self, domain=OutletDomain.Spec, outlet_name='Chiller')

        if not self.switch.detected:
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
        return self.switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.switch.detected:
            ret.append(f"chiller: power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        if not self.is_on():
            ret.append('chiller: not powered on')
        return ret

    def status(self):
        return {
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
        }
