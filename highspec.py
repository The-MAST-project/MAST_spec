import logging
import os.path
import time
from typing import List, Optional

import common.api
from cameras.andor.newton import camera as highspec_camera, NewtonActivities
from common.models.highspec import HighspecModel

from common.utils import Component, BASE_SPEC_PATH, CanonicalResponse_Ok
from common.config import Config
from common.activities import HighspecActivities, SpecActivities
from common.spec import SpecExposureSettings, BinningLiteral
from common.tasks.models import AssignedTaskModel, SpectrographModel, SpectrographAssignmentModel, TaskAcquisitionPathNotification
from fastapi.routing import APIRouter
from stage.stage import zaber_controller as stage_controller, Stage
from common.models.assignments import HighSpecAssignment, Initiator
from filter_wheel.wheel import Wheel, WheelActivities
from logging import Logger
from common.mast_logging import init_log
from pathlib import Path
from common.paths import PathMaker
from threading import Thread
from astropy.io import fits

logger = logging.Logger('highspec')
init_log(logger)

class HighspecAcquisitionSettings:
    """
    A series of images from the Newton camera
    """
    def __init__(self):
        self.folder: Path = Path(PathMaker().make_spec_acquisitions_folder(spec_name='highspec'))
        self.image_file = self.folder / PathMaker.make_seq(str(self.folder))


class Highspec(Component):

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Highspec, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.conf = Config().get_specs()['highspec']
        self.spec: 'Spec' = None    # the parent, instrument-independent part of the spectrograph
        Component.__init__(self)

        self.camera = highspec_camera
        self.focusing_stage = stage_controller.focusing_stage if hasattr(stage_controller, 'focusing_stage') else None
        self.disperser_stage = stage_controller.disperser_stage if hasattr(stage_controller, 'disperser_stage') else None

        self._initialized = True

    def set_parent(self, parent: 'Spec'):
        self.spec = parent

    @property
    def detected(self) -> bool:
        return self.camera.detected

    @property
    def connected(self) -> bool:
        return self.camera.connected

    @property
    def was_shut_down(self) -> bool:
        return self.camera.was_shut_down

    @property
    def why_not_operational(self) -> List[str]:
        return self.camera.why_not_operational

    @property
    def operational(self) -> bool:
        return self.camera.operational

    @property
    def name(self) -> str:
        return 'highspec'

    def startup(self):
        self.camera.startup()

    def shutdown(self):
        self.camera.shutdown()

    def status(self):
        return {
            'activities': self.activities,
            'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'camera': self.camera.status(),
        }

    def abort(self):
        if self.is_active(HighspecActivities.Exposing):
            self.camera.abort()
        if self.is_active(HighspecActivities.Positioning):
            self.focusing_stage.abort()
            self.disperser_stage.abort()

    def start_exposure(self, settings: SpecExposureSettings):
        pass


    def expose(self,
               seconds: float,
               x_binning: BinningLiteral,
               y_binning: BinningLiteral,
               number_of_exposures: Optional[int] = 1):
        settings: SpecExposureSettings = SpecExposureSettings(
            exposure_duration=seconds,
            number_of_exposures=number_of_exposures,
            x_binning=x_binning,
            y_binning=y_binning,
            output_folder=None,
        )
        # self.camera.acquire(settings,,

    @property
    def is_working(self) -> bool:
        return self.is_active(HighspecActivities.Acquiring)

    def do_execute_assignment(self, assignment: SpectrographAssignmentModel, spec):
        """
        Executes a highspec spectrograph assignment (runs in a separate Thread)
        :param assignment: the assignment, as received from the controller
        :param spec: the parent spectrograph object
        :return:
        """
        self.start_activity(HighspecActivities.Acquiring)
        highspec_model: HighspecModel = assignment.spec   # the highspec-specific part of the Union

        disperser_name = highspec_model.disperser
        if self.disperser_stage and not self.disperser_stage.at_preset(disperser_name):
            self.start_activity(HighspecActivities.Positioning, existing_ok=True)
            self.disperser_stage.move_to_preset(disperser_name)

        if self.focusing_stage and not self.focusing_stage.at_preset(disperser_name):
            self.start_activity(HighspecActivities.Positioning, existing_ok=True)
            self.focusing_stage.move_to_preset(disperser_name)

        if self.is_active(HighspecActivities.Positioning) or spec.is_moving:
            while self.focusing_stage.is_moving or self.disperser_stage.is_moving or spec.is_moving:
                time.sleep(0.5)
            self.end_activity(HighspecActivities.Positioning)

        self.camera.apply_settings(highspec_model.camera)

        acquisition_folder: Path = Path(PathMaker().make_spec_acquisitions_folder(spec_name='highspec'))
        acquisition_folder = acquisition_folder / PathMaker.make_seq(str(acquisition_folder))

        notification: TaskAcquisitionPathNotification = TaskAcquisitionPathNotification(
            initiator=Initiator.local_machine(),
            path=str(acquisition_folder),
            task_id=assignment.task.ulid
        )
        controller_api = common.api.ControllerApi()
        controller_api.client.get('task_acquisition_path_notification', {'notice': notification})

        spec_exposure_settings = SpecExposureSettings(exposure_duration=999)    # dummy exposure_duration, temporary
        logger.info(f"taking {highspec_model.camera.number_of_exposures} exposures")
        for seq in range(1, highspec_model.camera.number_of_exposures+1):
            spec_exposure_settings.image_file = os.path.join(acquisition_folder, f'exposure#{seq:03}.fits')
            self.camera.acquire(spec_exposure_settings)
            logger.info(f'waiting for end of exposure#{seq:03} ...')
            while self.camera.is_active(NewtonActivities.Acquiring):
                time.sleep(0.5)

            with fits.open(spec_exposure_settings.image_file, mode='update') as hdul:
                hdr = hdul[0].header
                hdr['PROGRAM'] = 'MAST'
                hdr['INSTRUMENT'] = 'Highspec'
                hdul.flush()
        self.end_activity(HighspecActivities.Acquiring)

    def can_execute(self, assignment: SpectrographAssignmentModel):
        if self.camera and self.camera.detected:
            return True, None
        else:
            return False, 'no camera detected'


    def execute_assignment(self, assignment: SpectrographAssignmentModel, spec):
        Thread(name='newton-acquisition', target=self.do_execute_assignment, args=[assignment, spec]).start()
        return CanonicalResponse_Ok


highspec = Highspec()
base_path = BASE_SPEC_PATH + '/highspec'
tag = 'Highspec'
router = APIRouter()

router.add_api_route(base_path + '/status', tags=[tag], endpoint=highspec.status)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=highspec.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=highspec.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=highspec.abort)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=highspec.expose, response_model=None)
