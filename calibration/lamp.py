from typing import List

from common.dlipowerswitch import SwitchedOutlet, OutletDomain
from common.utils import Component
from common.config import Config


class CalibrationLamp(Component, SwitchedOutlet):

    def __init__(self, name):
        self._name = f"lamp-{name}"
        Component.__init__(self)
        self.conf = Config().toml['lamp'][name]
        SwitchedPowerDevice.__init__(self, self.conf)

        SwitchedOutlet.__init__(self, domain=OutletDomain.Spec, outlet_name=self.name)
        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

    def __repr__(self):
        return f"<Lamp name={self.name}>"

    @property
    def detected(self) -> bool:
        return self.is_on()

    @property
    def connected(self) -> bool:
        return self.is_on()

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @property
    def status(self):
        return {
            'operational': self.operational,
            'why_not_operational': self.why_not_operational
        }

    @property
    def operational(self) -> bool:
        return self.switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.switch.detected:
            ret.append(f"{self.name}: power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        elif not self.is_on():
            ret.append(f"{self.name}: not powered")
        return ret

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
    def name(self) -> str:
        return self._name
