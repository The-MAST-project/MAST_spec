from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Thread
from typing import get_args

from fastapi.routing import APIRouter

from cameras.greateyes.greateyes import cameras as greateyes_cameras
from common.activities import DeepspecActivities, GreatEyesActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import (
    SpectrographAssignmentModel,
)
from common.models.greateyes import GreateyesSettingsModel
from common.models.spectrographs import SpectrographModel
from common.models.statuses import DeepspecStatus
from common.paths import PathMaker
from common.spec import (
    DeepspecBands,
    SpecExposureSettings,
)
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
        Component.__init__(self, DeepspecActivities)

        self.cameras = greateyes_cameras
        self.spec = spec

        self._initialized = True
        self._name = ""

    @property
    def detected(self) -> bool:
        # if len(self.cameras.keys()) != 4:
        #     return False

        # for _, camera in self.cameras.items():
        #     if camera is None or not camera.conf.enabled:
        #         continue
        #     if not camera.detected:  # type: ignore
        #         return False
        return True

    @property
    def connected(self) -> bool:
        for _, cam in self.cameras.items():
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
        for cam in self.cameras.values():
            if cam:
                cam.startup()  # type: ignore # threads?

    def shutdown(self):
        for cam in self.cameras.values():
            if cam:
                cam.shutdown()  # type: ignore # threads?

    def powerdown(self):
        active_cameras = [cam for cam in self.cameras.values() if cam is not None]
        if any(
            [
                cam
                for cam in active_cameras
                if cam.is_active(GreatEyesActivities.ShuttingDown)
            ]
        ):  # type: ignore
            logger.info(
                "waiting for cameras to finish shutting down before powering off..."
            )
        while any(
            [
                cam
                for cam in active_cameras
                if cam.is_active(GreatEyesActivities.ShuttingDown)
            ]
        ):  # type: ignore
            time.sleep(0.5)

        for cam in active_cameras:
            cam.powerdown()  # type: ignore # threads?

    def status(self) -> DeepspecStatus:
        ret = DeepspecStatus(
            detected=self.detected,
            connected=self.connected,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
            cameras={
                key: self.cameras[key].status() if self.cameras[key] else None  # type: ignore
                for key in self.cameras
            },
        )
        return ret

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
        x_binning: int = 1,
        y_binning: int = 1,
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
        band: str,
        seconds: float,
        x_binning: int = 1,
        y_binning: int = 1,
        number_of_exposures: int | None = 1,
    ) -> CanonicalResponse:
        Thread(
            target=self.do_camera_expose,
            args=[band, seconds, x_binning, y_binning, number_of_exposures],
        ).start()
        return CanonicalResponse_Ok

    def do_camera_expose(
        self,
        band: str,
        seconds: float,
        x_binning: int = 1,
        y_binning: int = 1,
        number_of_exposures: int | None = 1,
    ) -> CanonicalResponse:
        if band not in list(get_args(DeepspecBands)):
            return CanonicalResponse(
                errors=[
                    f"invalid band '{band}', must be one of {list(get_args(DeepspecBands))}"
                ]
            )

        if not self.cameras[band]:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        camera = self.cameras[band]
        assert camera
        if not camera.detected:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        folder = PathMaker().make_spec_exposures_folder(spec_name="deepspec", band=band)
        os.makedirs(folder, exist_ok=True)

        for exposure_number in range(number_of_exposures or 1):
            image_file = os.path.join(
                folder, "seq=" + PathMaker.make_seq(folder) + ".fits"
            )

            settings: GreateyesSettingsModel = GreateyesSettingsModel(
                bytes_per_pixel=1,
                crop=None,
                shutter=None,
                exposure_duration=seconds,
                number_of_exposures=number_of_exposures,
                binning={"x": x_binning, "y": y_binning},  # type: ignore
                image_file=image_file,
                temp=None,
                readout=None,
                probing=None,
            )

            camera.start_exposure(settings=settings)
            if camera.errors:
                return CanonicalResponse(
                    errors=[
                        f"exposure #{exposure_number} of {number_of_exposures}, failed: '{e}'"
                        for e in camera.errors
                    ]
                )

            while camera.is_active(GreatEyesActivities.Acquiring):
                time.sleep(0.5)

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

    def execute_assignment(self, remote_assignment: SpectrographAssignmentModel, spec):
        assert isinstance(remote_assignment.spec, SpectrographModel)
        # deepspec_model: DeepspecModel = remote_assignment.spec

        while spec.is_moving:  # the fiber stage
            time.sleep(0.5)

        acquisition_folder = Path(
            PathMaker().make_spec_acquisitions_folder(spec_name="deepspec")
        )

        assert remote_assignment.plan.ulid is not None
        notify_controller_about_task_acquisition_path(
            task_id=remote_assignment.plan.ulid,
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
        base_path = Const().BASE_SPEC_PATH + "/deepspec"
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
