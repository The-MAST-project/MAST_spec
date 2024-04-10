from typing import List

from dlipower.dlipower.dlipower import SwitchedPowerDevice
from utils import Component, Config


class Chiller(SwitchedPowerDevice, Component):

    def __init__(self):
        self.conf = Config.toml['chiller']
        SwitchedPowerDevice.__init__(self, self.conf)

        if not self.switch.detected:
            return

        if not self.is_on():
            self.power_on()

    def startup(self):
        pass

    def shutdown(self):
        pass

    def abort(self):
        pass

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
            ret.append(f"power switch '{self.switch.name}' (at '{self.switch.ipaddress}') not detected")
        if not self.is_on():
            ret.append('not powered on')
        return ret

    def status(self):
        return {'powered': self.is_on()}
