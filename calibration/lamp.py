from __future__ import annotations

from typing import TYPE_CHECKING, List

from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component

if TYPE_CHECKING:
    from spec import Spec


class CalibrationLamp(Component, SwitchedOutlet):
    def __init__(self, name: str, spec: Spec | None = None):
        self._name = name
        self.spec = spec
        Component.__init__(self)
        self.conf = Config().get_specs().lamps

        SwitchedOutlet.__init__(
            self, domain=OutletDomain.SpecOutlets, outlet_name=f"{self.name}Lamp"
        )
        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

    def __repr__(self):
        return f"<Lamp>(name={self.name})"

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
            "operational": self.operational,
            "why_not_operational": self.why_not_operational,
        }

    @property
    def operational(self) -> bool:
        if self.power_switch is None:
            return False
        return self.power_switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if self.power_switch is None:
            ret.append("power switch not detected")
        elif not self.power_switch.detected:
            ret.append(f"{self.name}: {self.power_switch} not detected")
        elif self.is_off():
            ret.append(
                f"{self.name}: {self.power_switch}:{self.outlet_names[0]} is OFF"
            )
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
