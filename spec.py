from __future__ import annotations

import logging
import threading
import time
from threading import Thread
from typing import Dict, List

from fastapi import APIRouter

import cooling.chiller
from calibration.lamp import CalibrationLamp
from common.canonical import CanonicalResponse, CanonicalResponse_Ok

# from common.config import Config
from common.const import Const
from common.dlipowerswitch import (
    DliPowerSwitch,
    OutletDomain,
    PowerSwitchFactory,
    SwitchedOutlet,
)
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import (
    SpectrographAssignmentModel,
)
from common.models.calibration import CalibrationModel
from common.models.statuses import SpecStatus
from common.spec import (
    Disperser,
    SpecAcquisitionSettings,
    SpecActivities,
    SpecExposureSettings,
    SpecId,
    SpecName,
)
from common.utils import function_name
from deepspec import Deepspec
from filter_wheel.wheel import FilterWheels, Wheel
from highspec import Highspec
from shutter.uniblitz import UniblitzController
from stage.stage import StageController

# The Newton HighSpec camera must be switched on before the Newton.startup() is called
highspec_outlet = SwitchedOutlet(
    domain=OutletDomain.SpecOutlets, outlet_name="Highspec"
)
assert highspec_outlet.power_switch is not None
if highspec_outlet.power_switch.detected:
    if highspec_outlet.is_off():
        highspec_outlet.power_on()


logger = logging.getLogger("spec")
init_log(logger)


class Spec(Component):
    """
    The main spectrograph object, managing the actual specs (deep and high), filter wheels, filters,
      stages, power switches, etc.
    """

    _instance: Spec | None = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Spec, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        Component.__init__(self, SpecActivities)
        self.logger = logging.Logger("spec")
        init_log(self.logger)

        self.power_switches: List[DliPowerSwitch] = [
            PowerSwitchFactory.get_instance("mast-spec-ps1"),
            PowerSwitchFactory.get_instance("mast-spec-ps2"),
        ]
        self.deepspec = Deepspec(self)
        self.highspec = Highspec(self)

        # convenience fields for the stages
        stage_controller = StageController(self)
        self.fiber_stage = stage_controller.fiber_stage
        self.disperser_stage = stage_controller.disperser_stage
        self.focusing_stage = stage_controller.focusing_stage

        self.wheels: List[Wheel] = FilterWheels(self).wheels
        self.thar_wheel = [w for w in self.wheels if w.name == "ThAr"][0]

        self.chiller = cooling.chiller.Chiller()
        self.lamps: List[CalibrationLamp] = [
            CalibrationLamp(name="ThAr", spec=self),
            CalibrationLamp(name="qTh", spec=self),
        ]
        self.thar_lamp = [lamp for lamp in self.lamps if lamp.name == "ThAr"][0]

        self.highspec_shutter = UniblitzController(spec=self, outlet_name="HighShutter")
        self.deepspec_shutter = UniblitzController(spec=self, outlet_name="DeepShutter")

        self.components_dict: Dict[str, Component | List[Component]] = {  # type: ignore
            "chiller": self.chiller,
            "power_switches": self.power_switches,
            "lamps": self.lamps,
            "deepspec": self.deepspec if hasattr(self, "deepspec") else None,
            "highspec": self.highspec if hasattr(self, "highspec") else None,
            "stages": stage_controller.stages,
            "wheels": self.wheels,
            "shutters": [self.highspec_shutter, self.deepspec_shutter],
        }

        self.components = []
        for k, v in self.components_dict.items():
            if isinstance(v, list):
                for item in v:
                    self.components.append(item)
            else:
                self.components.append(v)

        self.highspec_exposure_seconds = 15
        self.deepspec_exposure_seconds = 10

        self._name = "spec"

        self._was_shut_down = False
        self._initialized = True

    @property
    def detected(self) -> bool:
        return all([comp.detected for comp in self.components])

    @property
    def connected(self):
        return all([comp.connected for comp in self.components])

    @property
    def was_shut_down(self):
        return all([comp.was_shut_down for comp in self.components])

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def endpoint_status(self) -> CanonicalResponse:
        return CanonicalResponse(value=self.status())

    def status(self) -> SpecStatus:
        ret = self.traverse_components_and_return("status")
        ret |= {
            "powered": True,
            "detected": True,
            "activities": self.activities,
            "activities_verbal": self.activities_verbal,
            "operational": self.operational,
            "why_not_operational": self.why_not_operational,
        }
        return SpecStatus(**ret)

    def startup(self):
        self.traverse_components_and_call("startup")
        self._was_shut_down = False

    def shutdown(self):
        self.traverse_components_and_call("shutdown")
        self._was_shut_down = True

    @property
    def is_shutting_down(self) -> bool:
        return any([comp.is_shutting_down for comp in self.components])

    def powerdown(self):
        if not self._was_shut_down:
            logger.info("powerdown called without shutdown - calling shutdown first...")
            self.shutdown()
            time.sleep(3)

        if any(
            [
                comp
                for comp in self.components
                if comp is not None and comp.is_active(SpecActivities.ShuttingDown)
            ]
        ):
            logger.info(
                "waiting for components to finish shutting down before powering down..."
            )
            while any(
                [
                    comp
                    for comp in self.components
                    if comp is not None and comp.is_shutting_down
                ]
            ):
                time.sleep(0.5)
            logger.info("components finished shutting down, proceeding with powerdown")

        for comp in self.components:
            if hasattr(comp, "powerdown"):
                comp.powerdown()

    def abort(self):
        self.traverse_components_and_call("abort")

    def traverse_components_and_call(self, method_name: str):
        op = function_name()

        for key, component in self.components_dict.items():
            if isinstance(component, list):
                for comp in component:
                    if comp:
                        getattr(comp, method_name)()
                    else:
                        self.logger.error(
                            f"{op}: {key=}, {method_name=} - component is None"
                        )
            elif component is None:
                self.logger.error(f"{op}: {key=}, {method_name=} - component is None")
            else:
                getattr(component, method_name)()

    def traverse_components_and_return(self, method_name: str) -> dict:
        op = function_name()

        ret = {}
        for key, component in self.components_dict.items():
            if component is None:
                ret[key] = None
                continue

            if isinstance(component, list):
                if len(component) == 0:
                    ret[key] = []
                    continue

                ret[key] = {}
                name = ""
                for comp in component:
                    if comp is None:
                        ret[key] = None
                        continue

                    if isinstance(comp.name, str):
                        name = comp.name
                    elif callable(comp.name):
                        name = comp.name
                    try:
                        result = getattr(comp, method_name)
                        ret[key][name] = result() if callable(result) else result
                    except Exception as e:
                        self.logger.error(f"exception: {e} ({comp=}, {method_name=}")
                        pass
            elif component is not None:
                ret[key] = getattr(component, method_name)()
            else:
                self.logger.error(f"{op}: {key=}, {method_name=} - component is None")
        return ret

    @property
    def operational(self) -> bool:
        return all(map(lambda component: component.operational, self.components))

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        for comp in self.components:
            if comp is None:
                continue
            for reason in comp.why_not_operational:
                ret.append(reason)
        return ret

    def do_acquire(self, acquisition_settings: SpecAcquisitionSettings):
        """
        Performs the actual acquisition (running in a separate thread)
        Should be called after the required resources where checked and found operational.
        :return:
        """

        self.start_activity(SpecActivities.Acquiring)

        self.thar_lamp.power_on_or_off(acquisition_settings.lamp_on)
        #
        # NOTE:
        #  The positioning activity is started only once, to preserve timings
        #
        if acquisition_settings.spec == SpecId.Highspec:
            #
            # A Highspec acquisition
            #
            assert self.fiber_stage is not None
            assert self.disperser_stage is not None
            assert self.focusing_stage is not None
            assert acquisition_settings.grating is not None

            if self.fiber_stage.at_preset != "highspec":
                self.start_activity(SpecActivities.Positioning)
                self.fiber_stage.move_to_preset("Highspec")

            if self.disperser_stage.at_preset != acquisition_settings.grating:
                self.start_activity(SpecActivities.Positioning, existing_ok=True)
                self.disperser_stage.move_to_preset(acquisition_settings.grating)

            if self.focusing_stage.at_preset != acquisition_settings.grating:
                self.start_activity(SpecActivities.Positioning, existing_ok=True)
                self.focusing_stage.move_to_preset(acquisition_settings.grating)

            assert acquisition_settings.filter_name is not None
            if acquisition_settings.lamp_on:
                if not self.thar_wheel.at_filter(acquisition_settings.filter_name):
                    self.start_activity(SpecActivities.Positioning, existing_ok=True)
                    self.thar_wheel.move_to_filter(acquisition_settings.filter_name)

            if self.is_active(SpecActivities.Positioning):
                while any(
                    [
                        comp.is_moving
                        for comp in [
                            self.fiber_stage,
                            self.focusing_stage,
                            self.disperser_stage,
                            self.thar_wheel,
                        ]
                    ]
                ):
                    time.sleep(0.5)
                self.end_activity(SpecActivities.Positioning)
        else:
            #
            # A Deepspec acquisition
            #
            if (
                self.fiber_stage is not None
                and not self.fiber_stage.at_preset != "deepspec"
            ):
                self.start_activity(SpecActivities.Positioning)
                self.fiber_stage.move_to_preset("deepspec")
                while self.fiber_stage.is_moving:
                    time.sleep(0.5)
                self.end_activity(SpecActivities.Positioning)

        exposure_settings = SpecExposureSettings(
            exposure_duration=acquisition_settings.exposure_duration,
            number_of_exposures=acquisition_settings.number_of_exposures,
            x_binning=acquisition_settings.x_binning,
            y_binning=acquisition_settings.y_binning,
            folder=acquisition_settings.output_folder,
        )

        selected_spec = (
            self.highspec
            if acquisition_settings.spec == SpecId.Highspec
            else self.deepspec
        )
        self.start_activity(SpecActivities.Exposing)
        assert acquisition_settings.number_of_exposures is not None
        if acquisition_settings.number_of_exposures > 1:
            for i in range(acquisition_settings.number_of_exposures):
                exposure_settings.number_in_sequence = i
                selected_spec.start_acquisition(exposure_settings)
                while selected_spec.is_working:
                    time.sleep(2)
        else:
            exposure_settings.number_in_sequence = None
            selected_spec.start_acquisition(exposure_settings)
            while selected_spec.is_working:
                time.sleep(2)

        self.end_activity(SpecActivities.Acquiring)

    def acquire(
        self,
        spec_name: SpecName,
        exposure_duration: float,
        lamp_on: bool,
        number_of_exposures: int = 1,
        disperser: Disperser | None = None,
        filter_name: str | None = None,
        x_binning: int = 1,
        y_binning: int = 1,
        output_folder: str | None = None,
    ):
        """
        # Performs a spec acquisition
        * spec_name - One of the two spectrographs
        * exposure_duration - in seconds
        * lamp_on - whether to turm the ThAr lamp ON
        * number_of_exposures - how many exposures to take
        * grating - relevant only for a Highspec acquisition
        * filter_name - choose the ThAr filter (only if the lamp is turned on)
        * x_binning - horizontal binning
        * y_binning - vertical binning
        * output_folder - generated by the controller software
        """
        acquisition_settings: SpecAcquisitionSettings = SpecAcquisitionSettings(
            spec_name=spec_name,
            lamp_on=lamp_on,
            exposure_duration=exposure_duration,
            filter_name=filter_name,
            number_of_exposures=number_of_exposures,
            grating=disperser,
            x_binning=x_binning,
            y_binning=y_binning,
            output_folder=output_folder,
        )
        self.start_activity(SpecActivities.Checking)
        errors = []
        if lamp_on and filter_name is None:
            errors.append("lamp is required to be ON but filter_name is None")
        if not self.operational:
            errors.append(self.why_not_operational)

        if errors:
            self.end_activity(SpecActivities.Checking)
            return CanonicalResponse(errors=errors)
        self.end_activity(SpecActivities.Checking)

        threading.Thread(
            name="spec-acquisition", target=self.do_acquire, args=[acquisition_settings]
        ).start()
        return CanonicalResponse_Ok

    def set_params(self, highspec_seconds: float, deepspec_seconds: float):
        self.highspec_exposure_seconds = highspec_seconds
        self.deepspec_exposure_seconds = deepspec_seconds

    def do_execute_assignment(self, remote_assignment: SpectrographAssignmentModel):
        assert isinstance(remote_assignment.spec, SpectrographAssignmentModel)

        spec_assignment = remote_assignment.spec.spec
        executor = (
            self.highspec if spec_assignment.instrument == "highspec" else self.deepspec
        )

        assert spec_assignment.calibration is not None
        assert self.fiber_stage is not None

        calibration: CalibrationModel = spec_assignment.calibration
        thar_lamp = [lamp for lamp in self.lamps if lamp.name == "ThAr"][0]
        thar_wheel = [wheel for wheel in self.wheels if wheel.name == "ThAr"][0]

        if calibration.lamp_on:
            if not thar_lamp.is_on():
                thar_lamp.power_on()
            if calibration.filter and not self.thar_wheel.at_filter(calibration.filter):
                thar_wheel.move_to_filter(calibration.filter)
        else:
            thar_lamp.power_off()

        if (
            self.fiber_stage
            and self.fiber_stage.at_preset != spec_assignment.instrument
        ):
            self.fiber_stage.move_to_preset(spec_assignment.instrument)

        while self.fiber_stage.is_moving or thar_wheel.is_moving:
            time.sleep(0.5)

        executor.execute_assignment(remote_assignment, self)
        while executor.is_working:
            time.sleep(1)

    @property
    def is_moving(self) -> bool:
        if not self.thar_wheel or not self.fiber_stage:
            return False
        return self.thar_wheel.is_moving or self.fiber_stage.is_moving

    async def execute_assignment(self, remote_assignment: SpectrographAssignmentModel):
        initiator = remote_assignment.initiator
        what = f"remote assignment: from='{initiator.hostname}' ({initiator.ipaddr}), task='{remote_assignment.plan.ulid}'"

        assert isinstance(remote_assignment.spec, SpectrographAssignmentModel)

        if remote_assignment.plan.production and not self.operational:
            logger.info(
                f"REJECTED {what} (not operational: {self.why_not_operational})"
            )
            return CanonicalResponse(errors=self.why_not_operational)

        executor = (
            self.highspec
            if remote_assignment.spec.instrument == "highspec"
            else self.deepspec
        )
        assert isinstance(remote_assignment.spec, SpectrographAssignmentModel)
        can_execute, reasons = executor.can_execute(remote_assignment.spec)
        if not can_execute:
            logger.info(f"REJECTED {what} (reasons: {reasons})")
            return CanonicalResponse(errors=reasons)

        logger.info(f"ACCEPTED: {what}")
        Thread(target=self.do_execute_assignment, args=[remote_assignment]).start()
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH
        tag = "Spec"

        router = APIRouter()
        router.add_api_route(
            path=base_path + "/status", endpoint=self.endpoint_status, tags=[tag]
        )
        router.add_api_route(
            path=base_path + "/startup", endpoint=self.startup, tags=[tag]
        )
        router.add_api_route(
            path=base_path + "/shutdown", endpoint=self.shutdown, tags=[tag]
        )
        router.add_api_route(
            path=base_path + "/powerdown", endpoint=self.powerdown, tags=[tag]
        )
        router.add_api_route(
            path=base_path + "/acquire", endpoint=self.acquire, tags=[tag]
        )

        # tag = "Assignments"
        # router.add_api_route(
        #     path=base_path + "/execute_assignment",
        #     methods=["PUT"],
        #     endpoint=self.execute_assignment,
        #     tags=[tag],
        # )

        return router
