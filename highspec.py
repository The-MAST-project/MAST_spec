from __future__ import annotations

import logging
import os.path
import time
from pathlib import Path
from threading import Thread
from typing import List, Literal

import zaber_motion
from astropy.io import fits
from fastapi import Body
from fastapi.routing import APIRouter
from pydantic import ValidationError
from zaber_motion.units import LITERALS_TO_UNITS

from cameras.andor.newton import NewtonActivities, NewtonEMCCD
from cameras.qhy.qhy600 import (
    QHY600,
    QHYBinningModel,
    QHYCameraSettingsModel,
)
from common.activities import HighspecActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.newton import NewtonSettingsConfig
from common.config.shutter import ShutterConfig
from common.const import Const
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.assignments import SpectrographAssignmentModel
from common.models.highspec import HighspecModel
from common.models.statuses import HighspecStatus
from common.paths import PathMaker
from common.spec import SpecExposureSettings
from common.tasks.notifications import notify_controller_about_task_acquisition_path
from common.utils import function_name
from stage.stage import StageController as StageController
from stage.stage import UnitNames

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
    camera: Literal["newton", "qhy600", "as-configured"] = "qhy600"
    guessed_focus_position: float | None = (
        None  # None - start at current stage position
    )
    positions_per_step: float = 50  # stage steps between exposures
    unit: UnitNames = UnitNames("MILLIMETRES")
    gain: int | None = None
    number_of_exposures: int = 1
    lamp_on: bool = False  # ThAr lamp
    filters: list[str] | None = None  # optional list of filters
    gain: int | None = None  # for QHY600, em_gain for Newton


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
        try:
            self.conf = Config().get_specs().highspec
        except ValidationError as ex:
            logger.error(f"Bad highspec configuration: {ex=}")
            raise ValidationError from ex

        self.spec = spec  # the parent, instrument-independent part of the spectrograph
        Component.__init__(self, HighspecActivities)

        if self.conf.camera == "qhy600":
            from cameras.qhy.qhy600 import QHY600

            self.camera = QHY600()
        elif self.conf.camera == "newton":
            from cameras.andor.newton import NewtonEMCCD

            self.camera = NewtonEMCCD()
        else:
            raise ValueError(f"unknown configured camera '{self.conf.camera}'")

        self.camera.set_parent_spec(self)

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
        self.fiber_stage = (
            stage_controller.fiber_stage
            if hasattr(stage_controller, "fiber_stage")
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

    @property
    def is_shutting_down(self) -> bool:
        return self.camera.is_shutting_down

    def powerdown(self):
        self.camera.powerdown()

    def status(self) -> HighspecStatus:
        return HighspecStatus(
            detected=True,
            connected=self.connected,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            operational=self.operational,
            why_not_operational=self.why_not_operational,
            camera=self.camera.status(),
        )

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

    def do_autofocus(
        self,
        settings: HighspecAutofocusSettings = Body(
            default_factory=lambda: make_current_autofocus_settings()
        ),
    ) -> None:
        assert self.focusing_stage is not None

        match settings.camera:
            case "as-configured":
                pass  # use self.camera as is
            case "newton":
                self.camera = NewtonEMCCD()
            case "qhy600":
                self.camera = QHY600()
            case _:
                raise ValueError(
                    f"{function_name()}: unknown camera '{settings.camera}'"
                )

        self.start_activity(
            HighspecActivities.AutoFocusing,
            details=[
                f"around position {settings.guessed_focus_position}",
                f"unit {settings.unit}",
                f"{settings.number_of_exposures} exposures",
                f"step {settings.positions_per_step}",
            ],
        )

        if self.fiber_stage is not None:
            self.fiber_stage.move_to_preset("highspec")

        if self.spec is not None:
            self.spec.thar_lamp.power_on() if settings.lamp_on else self.spec.thar_lamp.power_off()
        else:
            settings.filters = None

        from stage.stage import reverse_units_dict

        if settings.guessed_focus_position is not None:
            starting_focus_position = settings.guessed_focus_position
            logger.debug(
                f"{function_name()}: using guessed focus position {starting_focus_position} {settings.unit}"
            )
        else:
            starting_focus_position = self.focusing_stage.position(
                unit=reverse_units_dict[settings.unit.name]
            )
            logger.debug(
                f"{function_name()}: no guessed focus position provided, using current stage position {starting_focus_position} {settings.unit}"
            )

        starting_focus_position -= (
            settings.positions_per_step
            * (settings.number_of_exposures - 1)  # number of steps to move back
        ) / 2  # type: ignore
        self.focusing_stage.move_absolute(
            starting_focus_position, unit=reverse_units_dict[settings.unit.name]
        )
        while self.focusing_stage.is_moving:
            time.sleep(0.5)

        # for filter in settings.filters or [None]:
        #     if (
        #         filter is not None
        #         and self.spec is not None
        #         and self.spec.thar_wheel is not None
        #     ):
        #         self.spec.thar_wheel.move_to_filter(filter_name=filter)

        folder = PathMaker().make_autofocus_folder()
        # if filter:
        #     folder = str(Path(folder) / f"filter={filter}")
        Path(folder).mkdir(parents=True, exist_ok=True)

        self.camera.set_parent_spec(self)

        for exposure_number in range(settings.number_of_exposures):
            unit_mnemonic = next(
                k
                for k, v in LITERALS_TO_UNITS.items()
                if v == reverse_units_dict[settings.unit.name]
            )
            image_path = (
                Path(folder)
                / f"FOCUS_{self.focusing_stage.position(unit=reverse_units_dict[settings.unit.name]):.5f}_{unit_mnemonic}.fits"
            )
            image_file = str(image_path)

            logger.debug(
                f"{function_name()}: Exposure #{exposure_number} out of {settings.number_of_exposures} into '{image_path.as_posix()}'"
            )
            if isinstance(self.camera, NewtonEMCCD):
                self.start_activity(HighspecActivities.Exposing)
                x_binning = settings.binning.x if settings.binning else 1
                y_binning = settings.binning.y if settings.binning else 1

                self.camera.start_acquisition(
                    settings=SpecExposureSettings(
                        exposure_duration=settings.exposure_duration,
                        x_binning=x_binning,
                        y_binning=y_binning,
                        image_path=image_file,
                        gain=settings.gain,
                    )
                )

            elif isinstance(self.camera, QHY600):
                self.start_activity(HighspecActivities.Exposing)
                binning = (
                    QHYBinningModel(
                        x=settings.binning.x,
                        y=settings.binning.y,
                    )
                    if settings.binning
                    else QHYBinningModel(x=1, y=1)
                )

                self.camera.start_single_exposure(
                    settings=QHYCameraSettingsModel(
                        exposure_duration=settings.exposure_duration,
                        binning=binning,
                        image_path=image_file,
                        gain=settings.gain,
                    )
                )

            while self.is_active(HighspecActivities.Exposing):
                time.sleep(0.5)

            if exposure_number < settings.number_of_exposures - 1:
                self.focusing_stage.move_relative(
                    settings.positions_per_step,
                    unit=reverse_units_dict[settings.unit.name],
                )
                while self.focusing_stage.is_moving:
                    time.sleep(0.5)

        if settings.lamp_on and self.spec is not None:
            self.spec.thar_lamp.power_off()

        #
        # Call Yahel's code to make known_as_good_focus_position
        # Update known_as_good_focus_position in config DB
        #
        self.end_activity(HighspecActivities.AutoFocusing)

    # class HighspecAutofocusSettings(NewtonSettingsConfig):
    #     camera: Literal["newton", "qhy600", "as-configured"] = "qhy600"
    #     guessed_focus_position: float | None = (
    #         None  # None - start at current stage position
    #     )
    #     positions_per_step: float = 50  # stage steps between exposures
    #     number_of_exposures: int = 1
    #     lamp_on: bool = False  # ThAr lamp
    #     filters: list[str] | None = None  # optional list of filters
    from stage.stage import reverse_units_dict

    def manual_autofocus(
        self,
        camera: Literal["newton", "qhy600"] = "qhy600",
        gain: int | None = None,
        exposure_duration: float = 1.0,
        guessed_focus_position: float | None = None,
        step_size: float = 5,
        unit: UnitNames = UnitNames("MILLIMETRES"),
        number_of_exposures: int = 1,
    ):
        settings = HighspecAutofocusSettings(
            camera=camera,
            guessed_focus_position=guessed_focus_position,
            exposure_duration=exposure_duration,
            positions_per_step=step_size,
            unit=unit,
            number_of_exposures=number_of_exposures,
            lamp_on=False,
            filters=None,
            gain=gain,
        )
        return self.autofocus(settings)

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
        return (
            self.is_active(HighspecActivities.Acquiring)
            or self.is_active(HighspecActivities.AutoFocusing)
            or self.is_active(HighspecActivities.Exposing)
        )

    def do_execute_assignment(
        self, remote_assignment: SpectrographAssignmentModel, spec
    ):
        """
        Executes a highspec spectrograph assignment (runs in a separate Thread)
        :param remote_assignment: the assignment, as received from the controller
        :param spec: the parent spectrograph object
        :return:
        """
        self.start_activity(HighspecActivities.Acquiring)
        assert isinstance(remote_assignment.spec, SpectrographAssignmentModel)
        assert isinstance(remote_assignment.spec.spec, HighspecModel)
        highspec_assignment: HighspecModel = (
            remote_assignment.spec.spec
        )  # the highspec-specific part of the Union

        disperser_name = highspec_assignment.disperser
        if self.disperser_stage and self.disperser_stage.at_preset != disperser_name:
            self.start_activity(HighspecActivities.Positioning, existing_ok=True)
            self.disperser_stage.move_to_preset(disperser_name)

        if self.focusing_stage and self.focusing_stage.at_preset != disperser_name:
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
        # self.camera.apply_settings(highspec_assignment.camera)

        acquisition_folder: Path = Path(
            PathMaker().make_spec_acquisitions_folder(spec_name="highspec")
        )
        acquisition_folder = acquisition_folder / PathMaker.make_seq(
            str(acquisition_folder)
        )

        assert remote_assignment.plan.file is not None
        notify_controller_about_task_acquisition_path(
            task_id=remote_assignment.plan.file,
            path_on_share=acquisition_folder,
            subpath="highspec",
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
            self.camera.start_acquisition(spec_exposure_settings)
            logger.info(f"waiting for end of exposure-{seq:03} ...")
            while self.camera.is_active(NewtonActivities.Acquiring):
                time.sleep(0.5)

            with fits.open(spec_exposure_settings.image_path, mode="update") as hdul:
                hdr = hdul[0].header  # type: ignore
                hdr["PROGRAM"] = "MAST"
                hdr["INSTRUME"] = "Highspec"
                hdul.flush()
        self.end_activity(HighspecActivities.Acquiring)

    def can_execute(self, assignment: SpectrographAssignmentModel):
        if self.camera and self.camera.detected:
            return True, None
        else:
            return False, ["no camera detected"]

    def execute_assignment(self, remote_assignment: SpectrographAssignmentModel, spec):
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
            base_path + "/manual_autofocus",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.manual_autofocus,
        )
        router.add_api_route(
            base_path + "/autofocus",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.autofocus,
        )

        return router


def make_current_autofocus_settings() -> HighspecAutofocusSettings:
    spec: Highspec = Highspec()

    return HighspecAutofocusSettings(
        camera=spec.conf.camera,
        guessed_focus_position=spec.focusing_stage.position(
            unit=zaber_motion.Units.LENGTH_MILLIMETRES
        )
        if spec.focusing_stage
        else None,
        positions_per_step=5,
        number_of_exposures=3,
        lamp_on=False,
        filters=None,
        shutter=spec.conf.shutter
        if spec.conf.shutter
        else ShutterConfig(open_time=12, close_time=9),
    )
