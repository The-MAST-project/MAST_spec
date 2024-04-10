from typing import List

from dlipower.dlipower.dlipower import SwitchedPowerDevice
from utils import Component, Config


class CalibrationLamp(Component, SwitchedPowerDevice):

    def __init__(self, name):
        self._name = f"lamp-{name}"
        Component.__init__(self)
        SwitchedPowerDevice.__init__(self, Config.toml['lamp'][name])

        if not self.is_on():
            self.power_on()

    def status(self):
        if not self.switch.detected:
            state = 'unknown'
        else:
            state = self.is_on()

        return {'powered': state}

    @property
    def operational(self) -> bool:
        return self.switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.switch.detected:
            ret.append(f"power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        elif not self.is_on():
            ret.append(f"not powered")
        return ret

    def startup(self):
        if not self.is_on():
            self.power_on()

    def shutdown(self):
        if self.is_on():
            self.power_off()

    def abort(self):
        pass

    def name(self) -> str:
        return self._name
