from typing import List, Literal, Optional

from cameras.greateyes.greateyes import GreatEyes, Band, cameras as greateyes_cameras

from common.utils import Component, BASE_SPEC_PATH
from common.config import Config
from common.activities import DeepspecActivities
from common.spec import SpecExposureSettings, BinningLiteral
from fastapi.routing import APIRouter

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

        self._initialized = True

    @property
    def detected(self) -> bool:
        if len(self.cameras.keys()) != 4:
            return False
        if any([not self.cameras[band].detected for band in self.cameras.keys()]):
            return False
        return True

    @property
    def connected(self) -> bool:
        return all([self.cameras[band].detected for band in self.cameras.keys()])

    @property
    def was_shut_down(self) -> bool:
        return all([self.cameras[band].was_shut_down for band in self.cameras.keys()])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        for band in self.cameras.keys():
            ret += self.cameras[band].why_not_operational
        return ret

    @property
    def operational(self) -> bool:
        return all([self.cameras[band].detected for band in self.cameras.keys()])

    @property
    def name(self) -> str:
        return 'deepspec'

    def startup(self):
        for band in self.cameras.keys():
            self.cameras[band].startup()    # threads?

    def shutdown(self):
        for band in self.cameras.keys():
            self.cameras[band].shutdown()    # threads?

    def status(self):
        return {
            'activities': self.activities,
            'activities_verbal': 'Idle' if self.activities == 0 else self.activities.__repr__(),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'cameras': {key: self.cameras[key].status() for key in self.cameras}
        }

    def abort(self):
        if self.is_active(DeepspecActivities.Exposing):
            for band in self.cameras.keys():
                self.cameras[band].abort()

    def start_exposure(self, settings: SpecExposureSettings):
        for band in self.cameras.keys():
            self.cameras[band].acquire(settings=settings)

    @property
    def is_working(self) -> bool:
        for cam in self.cameras.values():
            if cam.is_working:
                return True
        return False

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
            self.cameras[band].acquire(settings)

deepspec = Deepspec()
base_path = BASE_SPEC_PATH + 'deepspec'
tag = 'Deepspec'
router = APIRouter()

router.add_api_route(base_path + '/status', tags=[tag], endpoint=deepspec.status)
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=deepspec.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=deepspec.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=deepspec.abort)
router.add_api_route(base_path + '/expose', tags=[tag], endpoint=deepspec.expose, response_model=None)
