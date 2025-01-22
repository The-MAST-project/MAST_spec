import threading
from threading import Thread

import zaber_motion.units

import cooling.chiller
from common.utils import BASE_SPEC_PATH, Component, CanonicalResponse, CanonicalResponse_Ok, function_name
from common.config import Config
from common.mast_logging import init_log
from typing import List, Dict, Optional
from fastapi import APIRouter
from common.spec import SpecId, SpecName
from common.filer import Filer
from common.paths import PathMaker
import time
import logging
from common.dlipowerswitch import SwitchedOutlet, OutletDomain, DliPowerSwitch, PowerSwitchFactory
from common.spec import SpecExposureSettings, SpecActivities, SpecAcquisitionSettings, SpecGrating
from common.activities import HighspecActivities
from common.tasks.models import SpectrographAssignment
import os
from astropy.io import fits

spec_conf = None
if not spec_conf:
    spec_conf = Config().get_specs()

# The Newton HighSpec camera must be switched on before the Newton.startup() is called
highspec_outlet = SwitchedOutlet(domain=OutletDomain.Spec, outlet_name='Highspec')
if highspec_outlet.power_switch.detected:
    if highspec_outlet.is_off():
        highspec_outlet.power_on()

from cameras.andor.newton import camera as highspec_camera, NewtonEMCCD
from deepspec import deepspec
from stage.stage import zaber_controller as stage_controller, Stage
from filter_wheel.wheel import filter_wheeler, Wheel
from calibration.lamp import CalibrationLamp

logger = logging.getLogger('spec')
init_log(logger)

class Spec(Component):
    """
    The main spectrograph object, managing the actual specs (deep and high), filter wheels, filters,
      stages, power switches, etc.
    """

    def __init__(self):
        Component.__init__(self)
        self.logger = logging.Logger('spec')
        init_log(self.logger)

        self.power_switches: List[DliPowerSwitch] = [
            PowerSwitchFactory.get_instance('mast-spec-ps1'),
            PowerSwitchFactory.get_instance('mast-spec-ps2')
        ]
        self.deepspec = deepspec
        self.highspec: NewtonEMCCD = highspec_camera

        # convenience fields for the stages
        self.fiber_stage = stage_controller.fiber_stage if hasattr(stage_controller, 'fiber_stage') else None
        self.camera_stage = stage_controller.camera_stage if hasattr(stage_controller, 'camera_stage') else None
        self.gratings_stage = stage_controller.gratings_stage if hasattr(stage_controller, 'gratings_stage') else None

        self.wheels: List[Wheel] = filter_wheeler.wheels
        self.thar_wheel = [w for w in self.wheels if w.name == 'ThAr'][0]   # convenience wheel field

        self.chiller = cooling.chiller.Chiller()
        self.lamps: List[CalibrationLamp] = [
            CalibrationLamp('ThAr'),
            CalibrationLamp('qTh'),
        ]
        self.thar_lamp = [l for l in self.lamps if l.name == 'ThAr'][0]

        self.components_dict: Dict[str, Component | List[Component]] = {
            'chiller': self.chiller,
            'power_switches': self.power_switches,
            'lamps': self.lamps,
            'deepspec': self.deepspec if hasattr(self, 'deepspec') else None,
            'highspec': self.highspec if hasattr(self, 'highspec') else None,
            'stages': stage_controller.stages,
            'wheels': self.wheels,
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

        self._was_shut_down = False

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
        return 'spec'

    @property
    def status(self):
        ret = self.traverse_components_and_return('status')
        ret |= {
            'activities': self.activities,
            'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
        }
        return ret
    
    def startup(self):
        self.traverse_components_and_call('startup')
        self._was_shut_down = False
    
    def shutdown(self):
        self.traverse_components_and_call('shutdown')
        self._was_shut_down = True

    def abort(self):
        self.traverse_components_and_call('abort')

    def traverse_components_and_call(self, method_name: str):
        op = function_name()

        for key, component in self.components_dict.items():
            if isinstance(component, list):
                for comp in component:
                    if comp:
                        getattr(comp, method_name)()
                    else:
                        self.logger.error(f"{op}: {key=}, {method_name=} - component is None")
            elif component is None:
                self.logger.error(f"{op}: {key=}, {method_name=} - component is None")
            else:
                getattr(component, method_name)()

    def traverse_components_and_return(self, method_name: str) -> dict:
        op = function_name()

        ret = {}
        for key, component in self.components_dict.items():
            if isinstance(component, list):
                ret[key] = {}
                name = ''
                for comp in component:
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
            if not self.fiber_stage.at_preset('Highspec'):
                self.start_activity(SpecActivities.Positioning)
                self.fiber_stage.move_to_preset('Highspec')

            if not self.gratings_stage.at_preset(acquisition_settings.grating):
                self.start_activity(SpecActivities.Positioning, existing_ok=True)
                self.gratings_stage.move_to_preset(acquisition_settings.grating)

            if not self.camera_stage.at_preset(acquisition_settings.grating):
                self.start_activity(SpecActivities.Positioning, existing_ok=True)
                self.camera_stage.move_to_preset(acquisition_settings.grating)

            if acquisition_settings.lamp_on:
                if not self.thar_wheel.at_filter(acquisition_settings.filter_name):
                    self.start_activity(SpecActivities.Positioning, existing_ok=True)
                    self.thar_wheel.move_to_filter(acquisition_settings.filter_name)

            if self.is_active(SpecActivities.Positioning):
                while (self.fiber_stage.is_moving or self.camera_stage.is_moving or
                       self.gratings_stage.is_moving or self.thar_wheel.is_moving):
                    time.sleep(.5)
                self.end_activity(SpecActivities.Positioning)
        else:
            #
            # A Deepspec acquisition
            #
            if not self.fiber_stage.at_preset('Deepspec'):
                self.start_activity(SpecActivities.Positioning)
                self.fiber_stage.move_to_preset('Deepspec')
                while self.fiber_stage.is_moving:
                    time.sleep(.5)
                self.end_activity(SpecActivities.Positioning)

        exposure_settings = SpecExposureSettings(
            exposure_duration=acquisition_settings.exposure_duration,
            number_of_exposures=acquisition_settings.number_of_exposures,
            x_binning=acquisition_settings.x_binning,
            y_binning=acquisition_settings.y_binning,
            output_folder=acquisition_settings.output_folder,
        )

        working_spec = self.highspec if acquisition_settings.spec == SpecId.Highspec else self.deepspec
        self.start_activity(SpecActivities.Exposing)
        if acquisition_settings.number_of_exposures > 1:
            for i in range(acquisition_settings.number_of_exposures):
                exposure_settings.number_in_sequence = i
                working_spec.start_acquisition(exposure_settings)
                while working_spec.is_working:
                    time.sleep(2)
        else:
            exposure_settings.number_in_sequence = None
            working_spec.start_acquisition(exposure_settings)
            while working_spec.is_working:
                time.sleep(2)

        self.end_activity(SpecActivities.Acquiring)

    def acquire(self,
                spec_name: SpecName,
                exposure_duration: float,
                lamp_on: bool,
                number_of_exposures: Optional[int] = 1,
                grating: Optional[SpecGrating] = None,
                filter_name: Optional[str] = None,
                x_binning: Optional[int] = 1,
                y_binning: Optional[int] = 2,
                output_folder: Optional[str] = None,
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
        acquisition_settings: SpecAcquisitionSettings = SpecAcquisitionSettings(spec_name=spec_name,
                                                                                lamp_on=lamp_on,
                                                                                exposure_duration=exposure_duration,
                                                                                filter_name=filter_name,
                                                                                number_of_exposures=number_of_exposures,
                                                                                grating=grating,
                                                                                x_binning=x_binning,
                                                                                y_binning=y_binning,
                                                                                output_folder=output_folder)
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

        threading.Thread(name='spec-acquisition', target=self.do_acquire, args=[acquisition_settings]).start()
        return CanonicalResponse_Ok

    def set_params(self, highspec_seconds: float, deepspec_seconds: float):
        self.highspec_exposure_seconds = highspec_seconds
        self.deepspec_exposure_seconds = deepspec_seconds

    def take_highspec_exposures_for_focus(self,
                                          exposure_duration: float,
                                          iterations: int,
                                          x_binning: int,
                                          y_binning: int,
                                          stage_start_position_microns: float,
                                          stage_microns_per_step: float):
        """
        ### Take Highspec exposures for focus training
        Moves the Highspec focusing stage to a starting position then
         starts a series of exposures, moving the stage in between by a specified
         amount (microns)
        - exposure_duration - seconds (may be fractional)
        - iterations - How many steps in the series
        - x_binning - horizontal binning
        - y_binning - vertical binning
        - stage_start_position_microns - starting position for the focusing stage (float, microns)
        - stage_microns_per_step - how much to move the stage between exposures (float, microns)
        """
        Thread(target=self.do_take_highspec_exposures_for_focus, args=[
            exposure_duration, iterations,
            x_binning, y_binning,
            stage_start_position_microns, stage_microns_per_step
        ]).start()

    def do_take_highspec_exposures_for_focus(self,
                                          exposure_duration: float,
                                          iterations: int,
                                          x_binning: int,
                                          y_binning: int,
                                          stage_start_position_microns: float,
                                          stage_microns_per_step: float):

        self.start_activity(HighspecActivities.Focusing)
        logger.info(f"moving stage to starting position {stage_start_position_microns} ...")
        self.camera_stage.move_absolute(stage_start_position_microns, unit=zaber_motion.units.Units.LENGTH_MICROMETRES)
        while self.camera_stage.is_moving:
            time.sleep(1)
        logger.info(f"stage arrived to {self.camera_stage.position(unit=zaber_motion.units.Units.LENGTH_MICROMETRES)}...")

        folder = os.path.join(
            PathMaker().make_daily_folder_name(Filer().shared.root),
            'highspec_focus')
        folder = os.path.join(folder, PathMaker().make_seq(folder, None, start_with=1))

        settings = SpecExposureSettings(
            exposure_duration=exposure_duration,
            number_of_exposures=1,
            x_binning=x_binning,
            y_binning=y_binning,
            output_folder=folder,
        )

        for exposure_number in range(iterations):
            settings.image_file = f"stage_position={int(self.camera_stage.position(unit=zaber_motion.units.Units.LENGTH_MICROMETRES))}"
            self.highspec.start_acquisition(settings)
            while self.highspec.is_working:
                logger.info(f"highspec is still working ...")
                time.sleep(exposure_duration / 5)

            with fits.open(self.highspec.latest_settings.image_full_path, mode='update') as hdul:
                header = hdul[0].header
                header['FOCUS_NATIVE'] = (
                    self.camera_stage.position(unit=zaber_motion.units.Units.NATIVE),
                    'Focus position in native units')
                header['FOCUS_MICROMETER'] = (
                    self.camera_stage.position(unit=zaber_motion.units.Units.LENGTH_MICROMETRES),
                    'Focus position in micrometers')
                hdul.flush()

            self.camera_stage.move_relative(stage_microns_per_step, unit=zaber_motion.units.Units.LENGTH_MICROMETRES)
            while self.camera_stage.is_moving:
                time.sleep(1)
            logger.info(f"camera stage now at {self.camera_stage.position}")

        self.end_activity(HighspecActivities.Focusing)

    def do_perform_assignment(self, assignment: SpectrographAssignment):
        pass

    def receive_assignment(self, assignment: SpectrographAssignment):
        if not self.operational:
            return CanonicalResponse(errors=self.why_not_operational)
        self.do_perform_assignment(assignment)


spec = Spec()


def expose(spec_name: SpecName,
           duration: float,
           number_of_exposures: Optional[int] = 1,
           x_binning: Optional[int] = 1,
           y_binning: Optional[int] = 1,
           output_folder: Optional[str] = None,
           ):

    spec_id = SpecId[spec_name]
    spectrograph = spec.deepspec if spec_id == SpecId.Deepspec else spec.highspec
    settings = SpecExposureSettings(exposure_duration=duration, number_of_exposures=number_of_exposures,
                                    x_binning=x_binning, y_binning=y_binning, output_folder=output_folder)
    spectrograph.acquire(settings)


def status():
    return CanonicalResponse(value=spec.status)


def set_params(highspec_exposure: float, deepspec_exposure: float):
    spec.set_params(highspec_exposure, deepspec_exposure)

def startup():
    spec.startup()

def shutdown():
    spec.shutdown()


base_path = BASE_SPEC_PATH
tag = 'Spec'

router = APIRouter()
router.add_api_route(path=base_path + 'status', endpoint=status, tags=[tag])
router.add_api_route(path=base_path + 'startup', endpoint=spec.startup, tags=[tag])
router.add_api_route(path=base_path + 'shutdown', endpoint=spec.shutdown, tags=[tag])
router.add_api_route(path=base_path + 'setparams', endpoint=set_params, tags=[tag])
router.add_api_route(path=base_path + 'acquire', endpoint=spec.acquire, tags=[tag])
router.add_api_route(path=base_path + 'take_highspec_exposures_for_focus', endpoint=spec.take_highspec_exposures_for_focus, tags=[tag])
router.add_api_route(path=base_path + 'receive_assignment', endpoint=spec.receive_assignment, tags=[tag])
