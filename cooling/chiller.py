from fastapi.routing import APIRouter

from common.config import Config
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import register_component_endpoints
from common.interfaces.components import Component
from common.models.statuses import BaseStatus


class Chiller(SwitchedOutlet, Component):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.conf = Config().get_specs().chiller
        SwitchedOutlet.__init__(self, domain=OutletDomain.SpecOutlets, outlet_name="Chiller")
        self._name = "chiller"

        assert self.power_switch is not None
        if not self.power_switch.detected:
            return

        if not self.is_on():
            self.power_on()
        self._was_shut_down = False

        self._initialized = True

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
        self._was_shut_down = True

    @property
    def is_shutting_down(self) -> bool:
        return False  # chiller does not have a shutdown procedure, so never report as shutting down

    def powerdown(self):
        if self.is_on():
            self.power_off()

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
    def why_not_operational(self) -> list[str]:
        ret = []
        assert self.power_switch is not None
        if not self.power_switch.detected:
            ret.append(f"{self.name}: {self.power_switch} not detected")
        elif self.is_off():
            ret.append(f"{self.name}: {self.power_switch}:{self.outlet_names[0]} is OFF")
        return ret

    def status(self) -> BaseStatus:
        ret = BaseStatus(
            # powered=self.is_on(),
            detected=self.detected,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
        )
        return ret

    @property
    def api_router(self) -> APIRouter:
        # The separator was missing: BASE_SPEC_PATH + self.name gave
        # "/mast/api/v1/specchiller", not "/mast/api/v1/spec/chiller". Every other component
        # in this repo writes + "/<name>". Nothing called the malformed path -- the chiller's
        # status reaches control through Spec.status()'s component traversal, not over its own
        # route -- which is why it went unnoticed.
        base_path = Const().BASE_SPEC_PATH + f"/{self.name}"
        router = APIRouter()

        # All four interface verbs, generated from the Component ABC. Only /status was
        # registered before; startup, shutdown and abort exist on this class and were
        # reachable through Spec's traversal but had no routes of their own.
        register_component_endpoints(router, self, base_path)
        return router
