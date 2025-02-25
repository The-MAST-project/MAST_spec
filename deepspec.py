import time
from typing import List, Literal, Optional

import common.api
from cameras.greateyes.greateyes import GreatEyes, cameras as greateyes_cameras

from common.utils import Component, BASE_SPEC_PATH
from common.config import Config
from common.activities import DeepspecActivities
from common.spec import SpecExposureSettings, BinningLiteral
from fastapi.routing import APIRouter
from common.models.assignments import DeepSpecAssignment, SpectrographAssignmentModel, Initiator, RemoteAssignment
from common.models.deepspec import DeepspecModel
from common.mast_logging import init_log
from common.paths import PathMaker
from common.tasks.notifications import notify_controller_about_task_acquisition_path
import logging
from pathlib import Path

logger = logging.Logger('deepspec')
init_log(logger)

class Deepspec(Component):

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Deepspec, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.conf = Config().get_specs()['deepspec']
        Component.__init__(self)

        self.cameras = greateyes_cameras
        self.spec = None

        self._initialized = True

    def set_parent(self, parent: 'Spec'):
        self.spec = parent

    @property
    def detected(self) -> bool:
        if len(self.cameras.keys()) != 4:
            return False
        if any([not self.cameras[band].detected for band in self.cameras.keys()]):
            return False
        return True

    @property
    def connected(self) -> bool:
        for cam in self.cameras:
            if cam is None or not cam.connected:
                return False

        return True

    @property
    def was_shut_down(self) -> bool:
        return all([self.cameras[band].was_shut_down for band in self.cameras.keys()])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        for band in self.cameras.keys():
            if self.cameras[band] is not None:
                ret += self.cameras[band].why_not_operational
        return ret

    @property
    def operational(self) -> bool:
        for band in self.cameras.keys():
            if self.cameras[band] is None or not self.cameras[band].operational:
                return False
        return True

    @property
    def name(self) -> str:
        return 'deepspec'

    def startup(self):
        for band in self.cameras.keys():
            if self.cameras[band]:
                self.cameras[band].startup()    # threads?

    def shutdown(self):
        for band in self.cameras.keys():
            if self.cameras[band]:
                self.cameras[band].shutdown()    # threads?

    def status(self):
        return {
            'activities': self.activities,
            'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'cameras': {key: self.cameras[key].status()  if self.cameras[key] else None for key in self.cameras}
        }

    def abort(self):
        if self.is_active(DeepspecActivities.Acquiring):
            for band in self.cameras.keys():
                if self.cameras[band]:
                    self.cameras[band].abort()

    def start_exposure(self, settings: SpecExposureSettings):
        for band in self.cameras.keys():
            if self.cameras[band]:
                pass

    @property
    def is_working(self) -> bool:
        return self.is_active(DeepspecActivities.Acquiring)

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
        for band in self.cameras.keys():
            if self.cameras[band]:
                pass

    def can_execute(self, assignment: SpectrographAssignmentModel):
        """
        do we have at least one operational camera?
        :param assignment:
        :return:
        """
        errors = []
        for band in self.cameras.keys():
            if self.cameras[band] is None or not self.cameras[band].detected:
                continue
            if not self.cameras[band].operational:
                for err in self.cameras[band].why_not_operational:
                    errors.append(err)
                continue
        return (False, errors) if  errors else (True, None)

    def execute_assignment(self, remote_assignment: RemoteAssignment, spec: 'Spec' = None):
        deepspec_model: DeepspecModel = remote_assignment.assignment.spec

        while spec.is_moving:   # the fiber stage
            time.sleep(0.5)


        acquisition_folder = Path(PathMaker().make_spec_acquisitions_folder(spec_name='deepspec'))

        notify_controller_about_task_acquisition_path(task_id=remote_assignment.assignment.task.ulid,
                                                      src=acquisition_folder, link='deepspec')

        self.start_activity(DeepspecActivities.Acquiring)
        for band in list(self.cameras.keys()):
            camera = self.cameras[band]
            if not camera or not camera.detected:
                continue

            folder: Path = acquisition_folder / band
            self.cameras[band].execute_assignment(
                assignment=remote_assignment.assignment.spec,
                folder=acquisition_folder / band
            )

        while {band: cam for band, cam in self.cameras.items() if cam.is_working}:
            time.sleep(1)
        self.end_activity(DeepspecActivities.Acquiring)

deepspec = Deepspec()
base_path = BASE_SPEC_PATH + 'deepspec'
tag = 'Deepspec'
router = APIRouter()

router.add_api_route(base_path + '/status', tags=[tag], endpoint=deepspec.status)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=deepspec.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=deepspec.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=deepspec.abort)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=deepspec.expose, response_model=None)
