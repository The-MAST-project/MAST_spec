from __future__ import annotations

import os.path
import time
from pathlib import Path
from threading import Thread
from typing import Annotated

import zaber_motion
from astropy.io import fits
from fastapi import Body, Query
from fastapi.routing import APIRouter
from pydantic import ValidationError
from zaber_motion.units import LITERALS_TO_UNITS

from cameras.andor.newton import (
    NewtonAmplifierMode,
    NewtonEMCCD,
    NewtonHSSpeed,
    NewtonPreAmpGain,
    _pre_amp_gain_by_index,
)
from cameras.qhy.qhy600 import (
    QHY600,
    QHYActivities,
    QHYBinningModel,
    QHYCameraSettingsModel,
)
from common.activities import HighspecActivities, NewtonActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.shutter import ShutterConfig
from common.const import Const
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.assignments import AssignmentNotification, SpectrographAssignment
from common.models.highspec import HighspecSettings
from common.models.newton import NewtonSettingsConfig
from common.models.statuses import HighspecStatus
from common.notifications import Notifier
from common.paths import PathMaker
from common.spec import SpecActivities, SpecExposureSettings
from common.utils import function_name
from stage.stage import StageController, UnitNames

logger = get_logger(__name__)


# No `camera` field. Which camera this machine drives is `HighspecConfig.camera`, read
# once in Highspec.__init__; autofocus uses whatever that built, like every other
# endpoint. A per-call override is a second source of truth for a question the config
# already answers, and the cost of the single source is a service restart to change it.
class HighspecAutofocusSettings(NewtonSettingsConfig):
    guessed_focus_position: float | None = None  # None - start at current stage position
    positions_per_step: float = 50  # stage steps between exposures
    unit: UnitNames = UnitNames("MILLIMETRES")
    number_of_exposures: int = 1
    lamp_on: bool = False  # ThAr lamp
    filters: list[str] | None = None  # optional list of filters
    qhy600_gain: int | None = None
    # horizontal_shift_speed is inherited from NewtonSettingsConfig now that it is a
    # configured setting; redeclaring it here would only pin a second default.
    amplifier_mode: NewtonAmplifierMode = "em"
    em_gain: int = Query(default=240, ge=1, le=255)
    pre_amp_gain: NewtonPreAmpGain = NewtonPreAmpGain.x1
    bypass_temperature_stabilization_check: bool = False


class Highspec(Component):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
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
        self.focusing_stage = stage_controller.focusing_stage if hasattr(stage_controller, "focusing_stage") else None
        self.disperser_stage = stage_controller.disperser_stage if hasattr(stage_controller, "disperser_stage") else None
        self.fiber_stage = stage_controller.fiber_stage if hasattr(stage_controller, "fiber_stage") else None

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
    def why_not_operational(self) -> list[str]:
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
            camera_type=self.conf.camera,
            camera_status=self.camera.status(),
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
    #     settings: SpecExposureSettings = SpecExposureSettings(
    #         exposure_duration=seconds,
    #         number_of_exposures=number_of_exposures,
    #         x_binning=x_binning,
    #         y_binning=y_binning,
    #         folder=None,
    #     )
    #     # self.camera.acquire(settings,,

    def do_autofocus(
        self,
        autofocus_settings: HighspecAutofocusSettings = Body(default_factory=lambda: make_current_autofocus_settings()),
    ) -> None:
        assert self.focusing_stage is not None

        # `self.camera` as __init__ built it. This used to REASSIGN it from the request --
        # not for the run, permanently, with no restore -- and the endpoints registered in
        # api_router bind `self.camera.expose_single_image` once, at construction. So one
        # autofocus naming the other camera left /status reporting one camera while
        # /expose_single_image still exposed on the other.
        self.start_activity(
            HighspecActivities.AutoFocusing,
            details=[
                f"around position {autofocus_settings.guessed_focus_position}",
                f"unit {autofocus_settings.unit}",
                f"{autofocus_settings.number_of_exposures} exposures",
                f"step {autofocus_settings.positions_per_step}",
            ],
        )

        if self.fiber_stage is not None:
            self.fiber_stage.move_to_preset("highspec")

        if self.spec is not None:
            self.spec.thar_lamp.power_on() if autofocus_settings.lamp_on else self.spec.thar_lamp.power_off()
        else:
            autofocus_settings.filters = None

        from stage.stage import reverse_units_dict

        if autofocus_settings.guessed_focus_position is not None:
            starting_focus_position = autofocus_settings.guessed_focus_position
            logger.debug(
                f"{function_name()}: using guessed focus position {starting_focus_position} {autofocus_settings.unit}"
            )
        else:
            starting_focus_position = self.focusing_stage.position(unit=reverse_units_dict[autofocus_settings.unit.name])
            logger.debug(
                f"{function_name()}: no guessed focus position provided, using current stage position {starting_focus_position} {autofocus_settings.unit}"
            )

        starting_focus_position -= (
            autofocus_settings.positions_per_step
            * (autofocus_settings.number_of_exposures - 1)  # number of steps to move back
        ) / 2  # type: ignore
        # Refuse the sweep here rather than exposing at the wrong place. The stage now
        # rejects a target outside the axis travel instead of logging and returning, so a
        # sweep whose LOW end does not fit stops before its first frame. The high end is
        # caught by the same check on the step that reaches it, further down.
        response = self.focusing_stage.move_absolute(
            starting_focus_position,
            unit=reverse_units_dict[autofocus_settings.unit.name],
        )
        if response is not None and response.failed:
            logger.error(
                f"{function_name()}: cannot start the sweep at {starting_focus_position:.5f} "
                f"{autofocus_settings.unit.name}: {response.errors}"
            )
            self.end_activity(HighspecActivities.AutoFocusing)
            return
        while self.focusing_stage.is_moving:
            time.sleep(0.5)

        # for filter in settings.filters or [None]:
        #     if (
        #         filter is not None
        #         and self.spec is not None
        #         and self.spec.thar_wheel is not None
        #     ):
        #         self.spec.thar_wheel.move_to_filter(filter_name=filter)

        # `subfolder` names the instrument being focused. Without it this lands in
        # <date>/Autofocus/, the flat location MAST_unit uses for the TELESCOPE focuser --
        # so spec's HighSpec focus runs and a unit's would collide in one directory, and
        # each would read as the other's. The parameter was added in MAST_common#41 for
        # exactly this caller (MAST_unit#87 is the same mistake in the other direction).
        folder = PathMaker().make_autofocus_folder(subfolder=self.name)
        # if filter:
        #     folder = str(Path(folder) / f"filter={filter}")
        Path(folder).mkdir(parents=True, exist_ok=True)

        # Everything below writes into the ram-disk folder created above, so every exit
        # path must reach release_folder() -- see the finally.
        try:
            self.camera.set_parent_spec(self)

            self.start_activity(SpecActivities.ExposingHighspec)
            for exposure_number in range(autofocus_settings.number_of_exposures):
                logger.debug(
                    f"{function_name()} exposure_number: #{exposure_number} of {autofocus_settings.number_of_exposures}"
                )
                unit_mnemonic = next(
                    k for k, v in LITERALS_TO_UNITS.items() if v == reverse_units_dict[autofocus_settings.unit.name]
                )
                focus_position = self.focusing_stage.position(unit=reverse_units_dict[autofocus_settings.unit.name])
                if focus_position is None:
                    # position() returns None when it cannot read the axis. Nothing below can
                    # proceed without it -- the filename formats it, and the step check adds
                    # to it -- and the old code would have died formatting `{None:.5f}`.
                    logger.error(
                        f"{function_name()}: cannot read the focusing stage position; "
                        f"stopping the sweep after {exposure_number} of "
                        f"{autofocus_settings.number_of_exposures} exposures"
                    )
                    break

                # The exposure number is in the name because the position alone is not
                # unique. Whenever a step fails to move the axis, the next frame is taken at
                # the same position and used to be given the same name -- so it silently
                # overwrote its predecessor. A 3-exposure sweep on 2026-08-18 produced two
                # files: the +5 mm step from 24.99998 raised MotionLibException (past the
                # axis limit), stage.move_relative logged it and returned, and exposure #2
                # landed on exposure #1's path. Distinct names turn that into two frames at
                # one position -- visibly wrong, rather than invisibly missing.
                image_path = Path(folder) / f"FOCUS_{exposure_number:02}_{focus_position:.5f}_{unit_mnemonic}.fits"

                logger.debug(
                    f"{function_name()}: Exposure #{exposure_number} out of "
                    f"{autofocus_settings.number_of_exposures} into '{image_path.as_posix()}'"
                )
                if isinstance(self.camera, NewtonEMCCD):
                    self.start_activity(HighspecActivities.Exposing)

                    self.camera.expose_single_image(
                        exposure_duration=autofocus_settings.exposure_duration,
                        horizontal_shift_speed=autofocus_settings.horizontal_shift_speed,
                        amplifier_mode=autofocus_settings.amplifier_mode,
                        em_gain=autofocus_settings.em_gain,
                        pre_amp_gain=autofocus_settings.pre_amp_gain,
                        bypass_temperature_stabilization_check=autofocus_settings.bypass_temperature_stabilization_check,
                        image_full_path=image_path,
                    )

                elif isinstance(self.camera, QHY600):
                    self.start_activity(HighspecActivities.Exposing)
                    binning = (
                        QHYBinningModel(
                            x=autofocus_settings.binning.x,
                            y=autofocus_settings.binning.y,
                        )
                        if autofocus_settings.binning
                        else QHYBinningModel(x=1, y=1)
                    )

                    self.camera.start_single_exposure(
                        settings=QHYCameraSettingsModel(
                            exposure_duration=autofocus_settings.exposure_duration,
                            binning=binning,
                            image_path=str(image_path),
                            gain=autofocus_settings.qhy600_gain,
                        )
                    )

                # Wait for the activity that ends AFTER the file has been written, which is
                # a different one per camera:
                #
                #   Newton  -- `Acquiring`, ended by readout() once the FITS is saved.
                #              `Exposing` is no good here: the driver event handler ends it
                #              the instant the sensor goes idle and only THEN starts the
                #              readout thread, so this loop used to fall through and hand
                #              the mover a path that did not exist yet. The frame was
                #              written a moment later, protect() recorded it as a product,
                #              and release_folder() then waited 600 s for a product nothing
                #              would ever move. It is the signal _start_exposure_mover
                #              already waits on for the manual endpoint.
                #   QHY600  -- `ExposingSingleFrame`, ended after hdu.writeto().
                while (
                    self.camera.is_active(NewtonActivities.Acquiring)
                    if isinstance(self.camera, NewtonEMCCD)
                    else self.camera.is_active(QHYActivities.ExposingSingleFrame)
                ):
                    time.sleep(0.5)
                self.end_activity(HighspecActivities.Exposing)

                # This frame is finished: hand it to the mover. The camera protected the write,
                # which is what records it as a product for the release below.
                Filer().move_ram_to_shared(str(image_path))

                if exposure_number < autofocus_settings.number_of_exposures - 1:
                    step = autofocus_settings.positions_per_step
                    response = self.focusing_stage.move_relative(
                        step,
                        unit=reverse_units_dict[autofocus_settings.unit.name],
                    )
                    if response is not None and response.failed:
                        logger.error(
                            f"{function_name()}: {response.errors}; stopping the sweep after "
                            f"{exposure_number + 1} of {autofocus_settings.number_of_exposures} exposures"
                        )
                        break
                    while self.focusing_stage.is_moving:
                        time.sleep(0.5)

                    # Stop if the axis is not where the next exposure needs it. move_relative
                    # reports a refused move by logging and returning -- an out-of-range
                    # target raises MotionLibException inside it (stage.py), and both of
                    # _out_of_range()'s call sites are commented out, so nothing rejects the
                    # target up front either. Without this check the sweep carried on
                    # exposing at a position it had already used.
                    #
                    # Exposures already taken are kept: they are real frames at known
                    # positions, and the finally still hands the folder over so they reach
                    # the shared area.
                    target = focus_position + step
                    reached = self.focusing_stage.position(unit=reverse_units_dict[autofocus_settings.unit.name])
                    tolerance = max(abs(step) * 0.01, 1e-4)
                    if reached is None or abs(reached - target) > tolerance:
                        logger.error(
                            f"{function_name()}: focusing stage did not reach {target:.5f} {unit_mnemonic} "
                            f"(at {reached if reached is None else f'{reached:.5f}'}); "
                            f"stopping the sweep after {exposure_number + 1} of "
                            f"{autofocus_settings.number_of_exposures} exposures"
                        )
                        break

            self.end_activity(SpecActivities.ExposingHighspec)
            if autofocus_settings.lamp_on and self.spec is not None:
                self.spec.thar_lamp.power_off()

            #
            # Call Yahel's code to make known_as_good_focus_position
            # Update known_as_good_focus_position in config DB
            #
            self.end_activity(HighspecActivities.AutoFocusing)
        except Exception:
            logger.exception(f"{function_name()}: highspec autofocus failed")
        finally:
            # Reaped only once every protected frame has reached the shared area, and
            # never before -- a frame that failed to move keeps its folder rather than
            # being deleted with it.
            MoveGuardian().release_folder(folder, logger=logger)

    # Parameter ORDER is the grouping, as on expose_single_image: OpenAPI has no parameter
    # groups and Swagger UI renders one flat table, so adjacency plus a bold heading on
    # each group's first parameter is what stands in for sections.
    #
    # The camera-specific groups are BOTH shown whichever camera is configured, unlike
    # expose_single_image -- this endpoint is Highspec's own, not the camera's, so its
    # signature cannot vary with the camera. Hence each says which camera reads it.
    def manual_autofocus(
        self,
        # --- Focus sweep ---
        guessed_focus_position: Annotated[
            float | None,
            Query(
                description=(
                    "**--- Focus sweep ---**\n\n"
                    "Position to centre the sweep on. Omit to start from the stage's current "
                    "position."
                )
            ),
        ] = None,
        step_size: Annotated[
            float,
            Query(description="Distance between exposures, in `unit`."),
        ] = 5,
        unit: Annotated[
            UnitNames,
            Query(description="Unit that `guessed_focus_position` and `step_size` are given in."),
        ] = UnitNames("MILLIMETRES"),
        number_of_exposures: Annotated[
            int,
            Query(description="Exposures in the sweep, one per step."),
        ] = 3,
        # --- Exposure ---
        exposure_duration: Annotated[
            float,
            Query(description="**--- Exposure ---**\n\nExposure length (seconds), the same at every step."),
        ] = 1.0,
        bypass_temperature_stabilization_check: Annotated[
            bool,
            Query(description=("Sweep even if the sensor has not reached its target temperature. Not recommended.")),
        ] = False,
        # --- Newton: amplifier and readout ---
        #
        # horizontal_shift_speed is None-defaulted and falls back to the config; the three
        # below it are NOT, and so override the config on every call. They happen to carry
        # the configured values today, which is coincidence rather than coupling. Said
        # plainly in each description rather than papered over.
        horizontal_shift_speed: Annotated[
            NewtonHSSpeed | None,
            Query(
                description=(
                    "**--- Newton: amplifier and readout ---** *(ignored unless the configured "
                    "camera is a Newton)*\n\n"
                    "Readout (horizontal shift) speed. Together with `amplifier_mode` it decides "
                    "which `pre_amp_gain` values the camera offers. Omit to use the configured "
                    "speed -- the only parameter in this group that falls back to the config."
                )
            ),
        ] = None,
        amplifier_mode: Annotated[
            NewtonAmplifierMode,
            Query(
                description=(
                    "`em` reads out through the electron-multiplying register; `conventional` "
                    "bypasses it. Decides whether `em_gain` does anything. **Always sent**: "
                    "there is no 'use the configured mode' value, so this overrides the config "
                    "on every call."
                )
            ),
        ] = "em",
        pre_amp_gain: Annotated[
            NewtonPreAmpGain,
            Query(
                description=(
                    "Pre-amplifier gain, applied in **both** amplifier modes. Which values are "
                    "legal depends on `amplifier_mode` and `horizontal_shift_speed` together, "
                    "and an unavailable combination is refused before anything is applied. "
                    "**Always sent**, so it overrides the config on every call."
                )
            ),
        ] = NewtonPreAmpGain.x1,
        # --- Newton: EM mode only ---
        em_gain: Annotated[
            int,
            Query(
                description=(
                    "**--- Newton: EM mode only ---**\n\n"
                    "Gain of the electron-multiplying register. **Applied only when "
                    "`amplifier_mode` is `em`** -- in `conventional` mode it is accepted and "
                    "silently ignored. The 1..255 bound is the range of EM gain mode 0, not the "
                    "camera's advertised range. **Always sent**, so it overrides the config."
                ),
                ge=1,
                le=255,
            ),
        ] = 240,
        # --- QHY600 only ---
        qhy600_gain: Annotated[
            int | None,
            Query(
                description=(
                    "**--- QHY600 only ---** *(ignored unless the configured camera is a "
                    "QHY600)*\n\n"
                    "QHY600 sensor gain. No configured fallback: omitted, the gain is not set "
                    "and the camera keeps whatever it had."
                )
            ),
        ] = None,
    ):
        shift_speed = (
            horizontal_shift_speed if horizontal_shift_speed is not None else self.conf.settings.horizontal_shift_speed
        )
        settings = HighspecAutofocusSettings(
            guessed_focus_position=guessed_focus_position,
            exposure_duration=exposure_duration,
            positions_per_step=step_size,
            unit=unit,
            number_of_exposures=number_of_exposures,
            lamp_on=False,
            filters=None,
            qhy600_gain=qhy600_gain,
            amplifier_mode=amplifier_mode,
            em_gain=em_gain,
            pre_amp_gain=pre_amp_gain,
            horizontal_shift_speed=shift_speed,
            bypass_temperature_stabilization_check=bypass_temperature_stabilization_check,
        )
        return self.autofocus(settings)

    def autofocus(self, autofocus_settings: HighspecAutofocusSettings) -> CanonicalResponse:
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

    def do_execute_assignment(self, assignment: SpectrographAssignment, spec):
        """
        Executes a highspec spectrograph assignment (runs in a separate Thread)
        :param assignment: the assignment, as received from the controller
        :param spec: the parent spectrograph object
        :return:
        """
        self.start_activity(HighspecActivities.Acquiring)
        assert isinstance(assignment.spec, SpectrographAssignment)
        assert isinstance(assignment.spec.spec, HighspecSettings)
        highspec_assignment: HighspecSettings = assignment.spec.spec  # the highspec-specific part of the Union

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
            while self.focusing_stage.is_moving or self.disperser_stage.is_moving or spec.is_moving:
                time.sleep(0.5)
            self.end_activity(HighspecActivities.Positioning)

        assert highspec_assignment.camera is not None
        # self.camera.apply_settings(highspec_assignment.camera)

        acquisition_folder: Path = Path(PathMaker().make_spec_acquisitions_folder(spec_name="highspec"))
        acquisition_folder = acquisition_folder / PathMaker.make_seq(str(acquisition_folder))
        # `ram` is Optional only for the non-Windows Filer, where it is None; this folder was
        # just built under it by make_spec_acquisitions_folder, which asserts the same thing.
        # Narrowed here rather than reaching for `shared.root` to quiet the type checker:
        # that silences the warning and raises `ValueError: path is on mount 'D:', start on
        # mount 'Z:'` the first time an assignment runs.
        ram = Filer().ram
        assert ram is not None

        work = assignment.batch if assignment.batch is not None else assignment.plan if assignment.plan is not None else None
        assert work is not None and work.ulid is not None

        Notifier().assignment_notification(
            AssignmentNotification(
                assignment_id=str(work.ulid),
                state="in-progress",
                # Relative to the shared root, not the absolute ram path this folder is
                # written to: the controller symlinks it, and a `D:` path means nothing
                # there. `move_ram_to_shared` only swaps ram.root for shared.root, so the
                # ram-relative path is exactly where these products land. MAST_spec#39.
                shared_top=os.path.relpath(acquisition_folder, ram.root),
                shared_subpath="highspec",
            )
        )

        # From here on the ram-disk folder exists and exposures will be written into it, so
        # every exit path must reach release_folder() -- see the finally. The except is not
        # decoration either: this runs on the "newton-acquisition" thread, where an escaping
        # exception would go to threading's excepthook, i.e. to a stderr nobody reads.
        try:
            spec_exposure_settings = SpecExposureSettings(
                exposure_duration=999,
                image_full_name=str(acquisition_folder / "highspec" / "dummy.fits"),
            )  # dummy exposure_duration, temporary
            logger.info(f"taking {highspec_assignment.camera.number_of_exposures} exposures")
            assert isinstance(highspec_assignment.camera.number_of_exposures, int)
            spec.start_activity(SpecActivities.ExposingHighspec, data={"instrument": "highspec"})
            for seq in range(1, highspec_assignment.camera.number_of_exposures + 1):
                spec_exposure_settings.image_full_name = os.path.join(acquisition_folder, f"exposure-{seq:03}.fits")
                self.camera.start_acquisition(spec_exposure_settings)
                logger.info(f"waiting for end of exposure-{seq:03} ...")
                while self.camera.is_active(NewtonActivities.Acquiring):
                    time.sleep(0.5)

                # The camera protected the file while writing it; this header update is a
                # second write to the same path, so it is protected too -- otherwise the
                # move below could catch the file mid-flush.
                with (
                    MoveGuardian().protect(spec_exposure_settings.image_full_name),
                    fits.open(spec_exposure_settings.image_full_name, mode="update") as hdul,
                ):
                    hdr = hdul[0].header  # type: ignore
                    hdr["PROGRAM"] = "MAST"
                    hdr["INSTRUME"] = "Highspec"
                    hdul.flush()

                # The exposure is complete: hand it to the mover. Moving per exposure rather
                # than per folder keeps the local disk from holding a whole assignment, and
                # matches how MAST_unit moves each image once the flow knows it is done
                # (src/unit.py, src/solving.py) rather than from the camera's save path.
                Filer().move_ram_to_shared(spec_exposure_settings.image_full_name)

            self.end_activity(HighspecActivities.Acquiring)
            spec.end_activity(SpecActivities.ExposingHighspec)
        except Exception:
            logger.exception(f"{function_name()}: highspec assignment failed")
        finally:
            # Reaped only once every protected product has reached the shared area, and
            # never before -- an exposure that failed to move keeps its folder rather than
            # being deleted with it.
            MoveGuardian().release_folder(str(acquisition_folder), logger=logger)

    def can_execute(self, assignment: SpectrographAssignment) -> tuple[bool, list[str] | None]:
        if self.camera and self.camera.detected:
            if self.camera.temperature_is_stabilized:
                return True, None
            else:
                return False, ["camera detected but temperature not stabilized"]
        return False, ["no camera detected"]

    def execute_assignment(self, remote_assignment: SpectrographAssignment, spec):
        Thread(
            name="newton-acquisition",
            target=self.do_execute_assignment,
            args=[remote_assignment, spec],
        ).start()
        return CanonicalResponse_Ok

    def _configured_settings_markdown(self) -> str:
        """The values an omitted parameter falls back to, as a markdown table.

        Only the router can say this: parameter descriptions are built once at import, before
        any config is loaded, so a description cannot name a configured value. This runs at
        router construction, after Highspec.__init__ has read the config, which is the same
        reason the description can name the configured camera at all.

        Newton-only. A QHY600 has no configured exposure settings to fall back to -- its gain
        is either given or not applied -- so there would be nothing to tabulate.
        """
        if not isinstance(self.camera, NewtonEMCCD):
            return ""

        s = self.conf.settings
        rows = [
            ("exposure_duration", f"{s.exposure_duration} s"),
            ("amplifier_mode", f"`{s.amplifier_mode}`"),
            ("em_gain", str(s.em_gain)),
            ("pre_amp_gain", f"`{_pre_amp_gain_by_index[s.pre_amp_gain]}` (index {s.pre_amp_gain})"),
            ("horizontal_shift_speed", f"`{s.horizontal_shift_speed}`"),
        ]
        return (
            "**Configured values** -- what a parameter omitted below falls back to, "
            "where the parameter says it does:\n\n"
            "| setting | configured |\n|---|---|\n" + "".join(f"| `{name}` | {value} |\n" for name, value in rows)
        )

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + "/highspec"
        router = APIRouter()
        tag = "Highspec"

        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(base_path + "/shutdown", tags=[tag], endpoint=self.shutdown)
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)
        # Both cameras implement this, under this name, so it registers unconditionally.
        # Their parameters differ -- the Newton takes Andor hardware settings (amplifier
        # mode, EM gain, horizontal shift speed) that mean nothing to a QHY600 -- and that
        # is fine: FastAPI builds the schema from the bound method, so /docs on a given
        # machine describes the camera that machine actually has.
        #
        # It used to be `/expose` for the Newton and `/expose_single_image` for the QHY600,
        # registered behind an isinstance() check. A client therefore had to know which
        # camera was configured, and got a 404 when it guessed wrong -- or, hitting
        # `/expose` on a QHY600, a silent no-op, because QHY600.expose() was `pass`.
        # The router is built per instance, after the camera has been chosen, so it can say
        # which one this machine has. Without that, a reader of /docs sees a parameter list
        # (exposure_duration + Andor settings, or duration + gain) and has to infer the
        # camera from its shape.
        router.add_api_route(
            base_path + "/expose_single_image",
            tags=[tag],
            endpoint=self.camera.expose_single_image,
            methods=["PUT"],
            summary=f"Expose a single image ({self.conf.camera})",
            description=(
                f"Configured camera: **{self.conf.camera}** (`{type(self.camera).__name__}`).\n\n"
                "The parameters below are that camera's own. The two cameras do not share an "
                "exposure signature, so this schema describes the machine you are talking to, "
                "not the endpoint in general.\n\n" + self._configured_settings_markdown()
            ),
        )
        router.add_api_route(
            base_path + "/manual_autofocus",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.manual_autofocus,
            summary=f"Sweep the focusing stage, exposing at each step ({self.conf.camera})",
            description=(
                f"Configured camera: **{self.conf.camera}** (`{type(self.camera).__name__}`).\n\n"
                "Unlike `/expose_single_image`, this endpoint is Highspec's own rather than the "
                "camera's, so its signature cannot vary with the camera: **both** camera-specific "
                "parameter groups appear below whichever camera is configured, and the ones "
                "belonging to the other camera are ignored.\n\n"
                "On this endpoint only `horizontal_shift_speed` falls back to the configured "
                "value. The other Newton settings carry concrete defaults and are sent on every "
                "call, overriding the config whether or not you chose them -- each says so "
                "below.\n\n" + self._configured_settings_markdown()
            ),
        )
        router.add_api_route(
            base_path + "/autofocus",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.autofocus,
        )
        router.add_api_route(
            base_path + "/start_cooldown",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.camera.start_cooldown,
        )
        router.add_api_route(
            base_path + "/start_warmup",
            tags=[tag],
            methods=["PUT"],
            endpoint=self.camera.start_warmup,
        )

        return router


def make_current_autofocus_settings() -> HighspecAutofocusSettings:
    spec: Highspec = Highspec()

    return HighspecAutofocusSettings(
        guessed_focus_position=spec.focusing_stage.position(unit=zaber_motion.Units.LENGTH_MILLIMETRES)
        if spec.focusing_stage
        else None,
        positions_per_step=5,
        number_of_exposures=3,
        lamp_on=False,
        filters=None,
        shutter=spec.conf.shutter if spec.conf.shutter else ShutterConfig(open_time=12, close_time=9),
    )
