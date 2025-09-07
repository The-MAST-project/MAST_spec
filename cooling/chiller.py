from typing import List

from fastapi.routing import APIRouter

from common.config import Config
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component


class Chiller(SwitchedOutlet, Component):
    def __init__(self):
        self.conf = Config().get_specs().chiller
        SwitchedOutlet.__init__(
            self, domain=OutletDomain.SpecOutlets, outlet_name="Chiller"
        )
        self._name = "chiller"

        assert self.power_switch is not None
        if not self.power_switch.detected:
            return

        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

    def __repr__(self):
        return "Chiller()"

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

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
    def operational(self) -> bool:
        assert self.power_switch is not None
        return self.power_switch.detected and self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        assert self.power_switch is not None
        if not self.power_switch.detected:
            ret.append(f"{self.name}: {self.power_switch} not detected")
        elif self.is_off():
            ret.append(
                f"{self.name}: {self.power_switch}:{self.outlet_names[0]} is OFF"
            )
        return ret

    def status(self):
        return {
            "operational": self.operational,
            "why_not_operational": self.why_not_operational,
        }

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + self.name
        tag = "Chiller"
        router = APIRouter()

        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        return router


chiller = Chiller()
