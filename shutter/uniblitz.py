from __future__ import annotations

from typing import TYPE_CHECKING, List

from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component

if TYPE_CHECKING:
    from spec import Spec


class UniblitzController(Component, SwitchedOutlet):
    def __init__(self, outlet_name: str, spec: Spec | None = None):
        self.outlet_name = outlet_name
        self._name = outlet_name.replace("spec", "")
        self.spec = spec

        SwitchedOutlet.__init__(
            self, domain=OutletDomain.SpecOutlets, outlet_name=self.outlet_name
        )
        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

    def startup(self):
        self.power_on()
        self._was_shut_down = False

    def shutdown(self):
        self.power_off()
        self._was_shut_down = True

    def abort(self):
        pass

    def status(self):
        return {"powered": self.is_on()}

    @property
    def operational(self) -> bool:
        return self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        return ["not-powered"] if not self.is_on() else []

    @property
    def name(self) -> str:
        return self._name

    @property
    def detected(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return True

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down
