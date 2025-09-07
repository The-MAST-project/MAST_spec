from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi.routing import APIRouter

from cameras.greateyes.greateyes import cameras as greateyes_cameras
from common.activities import DeepspecActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import (
    SpectrographAssignmentModel,
    TransmittedAssignment,
)
from common.models.camera import BinningModel
from common.models.greateyes import GreateyesSettingsModel
from common.models.spectrographs import SpectrographModel
from common.paths import PathMaker
from common.spec import BinningLiteral, DeepspecBands, SpecExposureSettings
from common.tasks.notifications import notify_controller_about_task_acquisition_path

logger = logging.Logger("deepspec")
init_log(logger)


class Deepspec(Component):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Deepspec, cls).__new__(cls)
        return cls._instance

    def __init__(self, spec=None):
        if self._initialized:
            return

        self.conf = Config().get_specs().deepspec
        Component.__init__(self)

        self.cameras = greateyes_cameras
        self.spec = spec

        self._initialized = True
        self._name = ""

    @property
    def detected(self) -> bool:
        if len(self.cameras.keys()) != 4:
            return False

        for camera in self.cameras:
            if camera is None or not camera.detected:  # type: ignore
                return False
        return True

    @property
    def connected(self) -> bool:
        for cam in self.cameras:
            if cam is None or not cam.connected:  # type: ignore
                return False

        return True

    @property
    def was_shut_down(self) -> bool:
        return all([self.cameras[band].was_shut_down for band in self.cameras.keys()])  # type: ignore

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        for band in self.cameras.keys():
            if self.cameras[band] is not None:
                ret += self.cameras[band].why_not_operational  # type: ignore
        return ret

    @property
    def operational(self) -> bool:
        for band in self.cameras.keys():
            if self.cameras[band] is None or not self.cameras[band].operational:  # type: ignore
                return False
        return True

    @property
    def name(self) -> str:
        return "deepspec"

    def startup(self):
        for band in self.cameras.keys():
            if self.cameras[band]:
                self.cameras[band].startup()  # type: ignore # threads?

    def shutdown(self):
        for band in self.cameras.keys():
            if self.cameras[band]:
                self.cameras[band].shutdown()  # type: ignore # threads?

    def status(self):
        return {
            "activities": self.activities,
            "activities_verbal": "Idle"
            if self.activities == 0
            else self.activities.__repr__(),
            "operational": self.operational,
            "why_not_operational": self.why_not_operational,
            "cameras": {
                key: self.cameras[key].status() if self.cameras[key] else None  # type: ignore
                for key in self.cameras
            },
        }

    def abort(self):
        if self.is_active(DeepspecActivities.Acquiring):
            for band in self.cameras.keys():
                if self.cameras[band]:
                    self.cameras[band].abort()  # type: ignore

    def start_acquisition(self, settings: SpecExposureSettings):
        for band in self.cameras.keys():
            if self.cameras[band]:
                pass

    @property
    def is_working(self) -> bool:
        return self.is_active(DeepspecActivities.Acquiring)

    def expose(
        self,
        seconds: float,
        x_binning: BinningLiteral,
        y_binning: BinningLiteral,
        number_of_exposures: int | None = 1,
    ):
        settings: SpecExposureSettings = SpecExposureSettings(  # noqa: F841
            exposure_duration=seconds,
            number_of_exposures=number_of_exposures,
            x_binning=x_binning,
            y_binning=y_binning,
            folder=None,
        )
        for band in self.cameras.keys():
            if self.cameras[band]:
                pass

    def camera_expose(
        self,
        band: DeepspecBands,
        seconds: float,
        x_binning: BinningLiteral,
        y_binning: BinningLiteral,
        number_of_exposures: int | None = 1,
    ) -> CanonicalResponse:
        if not self.cameras[band]:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        camera = self.cameras[band]
        assert camera
        if not camera.detected:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        settings: GreateyesSettingsModel = GreateyesSettingsModel(
            bytes_per_pixel=1,
            crop=None,
            shutter=None,
            exposure_duration=seconds,
            number_of_exposures=number_of_exposures,
            binning=BinningModel(x=x_binning, y=y_binning),  # type: ignore
            temp=None,
            readout=None,
            probing=None,
        )

        camera.expose(settings=settings)
        return CanonicalResponse_Ok

    def can_execute(self, assignment: SpectrographAssignmentModel):
        """
        do we have at least one operational camera?
        :param assignment:
        :return:
        """
        errors = []
        for band in self.cameras.keys():
            if self.cameras[band] is None or not self.cameras[band].detected:  # type: ignore
                continue
            if not self.cameras[band].operational:  # type: ignore
                for err in self.cameras[band].why_not_operational:  # type: ignore
                    errors.append(err)
                continue
        return (False, errors) if errors else (True, None)

    def execute_assignment(self, remote_assignment: TransmittedAssignment, spec):
        assert isinstance(remote_assignment.assignment, SpectrographModel)
        # deepspec_model: DeepspecModel = remote_assignment.assignment.spec

        while spec.is_moving:  # the fiber stage
            time.sleep(0.5)

        acquisition_folder = Path(
            PathMaker().make_spec_acquisitions_folder(spec_name="deepspec")
        )

        assert remote_assignment.assignment.task.ulid is not None
        notify_controller_about_task_acquisition_path(
            task_id=remote_assignment.assignment.task.ulid,
            src=acquisition_folder,
            link="deepspec",
        )

        self.start_activity(DeepspecActivities.Acquiring)
        for band in list(self.cameras.keys()):
            camera = self.cameras[band]
            if not camera or not camera.detected:
                continue

            camera.execute_assignment(
                assignment=remote_assignment.assignment.spec,  # type: ignore
                folder=str(acquisition_folder / band),
            )

        while {
            band: cam
            for band, cam in self.cameras.items()
            if (cam is not None and cam.is_working)
        }:
            time.sleep(1)
        self.end_activity(DeepspecActivities.Acquiring)

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + "deepspec"
        tag = "Deepspec"
        router = APIRouter()

        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(
            base_path + "/shutdown", tags=[tag], endpoint=self.shutdown
        )
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)
        router.add_api_route(
            base_path + "/expose", tags=[tag], endpoint=self.expose, response_model=None
        )
        router.add_api_route(
            base_path + "/camera_expose",
            tags=[tag],
            endpoint=self.camera_expose,
            response_model=None,
        )

        return router


deepspec = Deepspec()
