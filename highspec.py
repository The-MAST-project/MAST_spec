from __future__ import annotations

import logging
import os.path
import time
from pathlib import Path
from threading import Thread
from typing import List

from astropy.io import fits
from fastapi.routing import APIRouter

from cameras.andor.newton import NewtonActivities, NewtonEMCCD
from common.activities import HighspecActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.newton import NewtonSettingsConfig
from common.const import Const
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import TransmittedAssignment
from common.models.highspec import HighspecModel
from common.paths import PathMaker
from common.spec import SpecExposureSettings
from common.tasks.models import SpectrographAssignmentModel
from common.tasks.notifications import notify_controller_about_task_acquisition_path
from stage.stage import StageController as StageController

logger = logging.Logger("highspec")
init_log(logger)


class HighspecAcquisitionSettings:
    """
    A series of images from the Newton camera
    """

    def __init__(self):
        self.folder: Path = Path(
            PathMaker().make_spec_acquisitions_folder(spec_name="highspec")
        )
        self.image_file = self.folder / PathMaker.make_seq(str(self.folder))


class HighspecAutofocusSettings(NewtonSettingsConfig):
    start_position: int | None = None  # None - start at current stage position
    step_positions: int = 50  # stage steps between exposures
    lamp_on: bool = False  # ThAr lamp
    filters: list[str] | None = None  # optional list of filters


class Highspec(Component):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Highspec, cls).__new__(cls)
        return cls._instance

    def __init__(self, spec=None):
        if self._initialized:
            return

        self._name = "highspec"
        self.conf = Config().get_specs().highspec
        self.spec = spec  # the parent, instrument-independent part of the spectrograph
        Component.__init__(self)

        self.camera = NewtonEMCCD()

        stage_controller = StageController(self.spec)
        self.focusing_stage = (
            stage_controller.focusing_stage
            if hasattr(stage_controller, "focusing_stage")
            else None
        )
        self.disperser_stage = (
            stage_controller.disperser_stage
            if hasattr(stage_controller, "disperser_stage")
            else None
        )

        self._initialized = True

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
        return "highspec"

    def startup(self):
        self.camera.startup()

    def shutdown(self):
        self.camera.shutdown()

    def status(self):
        return {
            "activities": self.activities,
            "activities_verbal": "Idle"
            if self.activities == 0
            else self.activities.__repr__(),
            "operational": self.operational,
            "why_not_operational": self.why_not_operational,
            "camera": self.camera.status(),
        }

    def abort(self):
        if self.is_active(HighspecActivities.Exposing):
            self.camera.abort()
        if self.is_active(HighspecActivities.Positioning):
            assert self.focusing_stage is not None
            assert self.disperser_stage is not None
            self.focusing_stage.abort()
            self.disperser_stage.abort()

    def start_acquisition(self, settings: SpecExposureSettings):
        raise NotImplementedError

    # def expose(
    #     self,
    #     seconds: float,
    #     x_binning: BinningLiteral,
    #     y_binning: BinningLiteral,
    #     number_of_exposures: Optional[int] = 1,
    # ):
    #     settings: SpecExposureSettings = SpecExposureSettings(  # noqa: F841
    #         exposure_duration=seconds,
    #         number_of_exposures=number_of_exposures,
    #         x_binning=x_binning,
    #         y_binning=y_binning,
    #         folder=None,
    #     )
    #     # self.camera.acquire(settings,,

    def do_autofocus(self, autofocus_settings: HighspecAutofocusSettings):
        assert self.focusing_stage is not None

        self.start_activity(HighspecActivities.Focusing)

        if autofocus_settings.lamp_on and self.spec is not None:
            self.spec.thar_lamp.power_on()
        else:
            autofocus_settings.filters = None

        for filter in autofocus_settings.filters or [None]:
            if filter is not None and self.spec is not None:
                self.spec.thar_wheel.move_to_filter(filter_name=filter)

            if autofocus_settings.start_position is not None:
                self.focusing_stage.move_absolute(autofocus_settings.start_position)
                while self.focusing_stage.is_moving:
                    time.sleep(0.5)

            folder = PathMaker().make_autofocus_folder()
            if filter:
                folder = str(Path(folder) / f"filter={filter}")
            Path(folder).mkdir(parents=True, exist_ok=True)

            for _ in range(autofocus_settings.number_of_exposures):
                image_path = str(Path(folder) / f"FOCUS_{self.focusing_stage.position}")

                settings: SpecExposureSettings = SpecExposureSettings(
                    exposure_duration=autofocus_settings.exposure_duration,
                    x_binning=autofocus_settings.binning.x
                    if autofocus_settings.binning
                    else 1,
                    y_binning=autofocus_settings.binning.y
                    if autofocus_settings.binning
                    else 1,
                    image_path=image_path,
                )

                self.camera.start_acquisition(settings=settings)
                while self.camera.is_active(NewtonActivities.Acquiring):
                    time.sleep(0.5)

                self.focusing_stage.move_relative(autofocus_settings.step_positions)
                while self.focusing_stage.is_moving:
                    time.sleep(0.5)

        if autofocus_settings.lamp_on and self.spec is not None:
            self.spec.thar_lamp.power_off()
        self.end_activity(HighspecActivities.Focusing)

    def autofocus(
        self, autofocus_settings: HighspecAutofocusSettings
    ) -> CanonicalResponse:
        if not self.operational:
            return CanonicalResponse(errors=self.why_not_operational)

        Thread(
            target=self.do_autofocus,
            args=[
                autofocus_settings,
            ],
        ).start()
        return CanonicalResponse_Ok

    @property
    def is_working(self) -> bool:
        return self.is_active(HighspecActivities.Acquiring) or self.is_active(
            HighspecActivities.Focusing
        )

    def do_execute_assignment(self, remote_assignment: TransmittedAssignment, spec):
        """
        Executes a highspec spectrograph assignment (runs in a separate Thread)
        :param remote_assignment: the assignment, as received from the controller
        :param spec: the parent spectrograph object
        :return:
        """
        self.start_activity(HighspecActivities.Acquiring)
        assert isinstance(remote_assignment.assignment, SpectrographAssignmentModel)
        assert isinstance(remote_assignment.assignment.spec, HighspecModel)
        highspec_assignment: HighspecModel = (
            remote_assignment.assignment.spec
        )  # the highspec-specific part of the Union

        disperser_name = highspec_assignment.disperser
        if self.disperser_stage and not self.disperser_stage.at_preset(disperser_name):
            self.start_activity(HighspecActivities.Positioning, existing_ok=True)
            self.disperser_stage.move_to_preset(disperser_name)

        if self.focusing_stage and not self.focusing_stage.at_preset(disperser_name):
            self.start_activity(HighspecActivities.Positioning, existing_ok=True)
            self.focusing_stage.move_to_preset(disperser_name)

        assert self.focusing_stage is not None
        assert self.disperser_stage is not None
        if self.is_active(HighspecActivities.Positioning) or spec.is_moving:
            while (
                self.focusing_stage.is_moving
                or self.disperser_stage.is_moving
                or spec.is_moving
            ):
                time.sleep(0.5)
            self.end_activity(HighspecActivities.Positioning)

        assert highspec_assignment.camera is not None
        self.camera.apply_settings(highspec_assignment.camera)

        acquisition_folder: Path = Path(
            PathMaker().make_spec_acquisitions_folder(spec_name="highspec")
        )
        acquisition_folder = acquisition_folder / PathMaker.make_seq(
            str(acquisition_folder)
        )

        assert remote_assignment.assignment.task.file is not None
        notify_controller_about_task_acquisition_path(
            task_id=remote_assignment.assignment.task.file,
            src=acquisition_folder,
            link="highspec",
        )

        spec_exposure_settings = SpecExposureSettings(
            exposure_duration=999
        )  # dummy exposure_duration, temporary
        logger.info(
            f"taking {highspec_assignment.camera.number_of_exposures} exposures"
        )
        assert highspec_assignment.camera.number_of_exposures is not None
        for seq in range(1, highspec_assignment.camera.number_of_exposures + 1):
            spec_exposure_settings.image_path = os.path.join(
                acquisition_folder, f"exposure-{seq:03}.fits"
            )
            self.camera.acquire(spec_exposure_settings)
            logger.info(f"waiting for end of exposure-{seq:03} ...")
            while self.camera.is_active(NewtonActivities.Acquiring):
                time.sleep(0.5)

            with fits.open(spec_exposure_settings.image_path, mode="update") as hdul:
                hdr = hdul[0].header  # type: ignore
                hdr["PROGRAM"] = "MAST"
                hdr["INSTRUMENT"] = "Highspec"
                hdul.flush()
        self.end_activity(HighspecActivities.Acquiring)

    def can_execute(self, assignment: SpectrographAssignmentModel):
        if self.camera and self.camera.detected:
            return True, None
        else:
            return False, ["no camera detected"]

    def execute_assignment(self, remote_assignment: TransmittedAssignment, spec):
        Thread(
            name="newton-acquisition",
            target=self.do_execute_assignment,
            args=[remote_assignment, spec],
        ).start()
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + "/highspec"
        router = APIRouter()
        tag = "Highspec"

        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(
            base_path + "/shutdown", tags=[tag], endpoint=self.shutdown
        )
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)
        # router.add_api_route(
        #     base_path + "/expose",
        #     tags=[tag],
        #     endpoint=self.expose,
        #     response_model=None,
        # )
        router.add_api_route(
            base_path + "/autofocus", tags=[tag], endpoint=self.autofocus
        )

        return router
