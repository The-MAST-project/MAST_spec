from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, get_args

from fastapi import Query
from fastapi.routing import APIRouter

from cameras.greateyes.greateyes import GreatEyes
from cameras.greateyes.greateyes import cameras as greateyes_cameras
from common.activities import DeepspecActivities, GreatEyesActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.filer import Filer, MoveGuardian
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.assignments import (
    AssignmentNotification,
    SpectrographAssignment,
)
from common.models.greateyes import (
    GreateyesSettingsModel,
    ReadoutAmplifiersMapping,
    ReadoutAmplifiersNames,
    ReadoutModel,
    ReadoutSpeedMapping,
    ReadoutSpeedNames,
    ShutterModel,
)
from common.models.spectrographs import SpectrographModel
from common.models.statuses import DeepspecStatus
from common.notifications import Notifier
from common.paths import PathMaker
from common.spec import (
    DeepspecBands,
    FrameType,
    SpecActivities,
    SpecExposureSettings,
)
from common.utils import function_name

logger = get_logger(__name__)


def _hand_exposure_to_mover(camera: GreatEyes, exposure_number: int) -> None:
    """Hand a finished exposure to the ram->shared mover.

    save_image() protected the write, which is what records it as a product for
    release_folder(). Moves the path actually written, held in `latest_saved_image_path`:
    save_image may append ".fits" or a frame-type tag, so the requested `image_file` is not
    reliably the one on disk.
    """
    if camera.latest_saved_image_path:
        Filer().move_ram_to_shared(camera.latest_saved_image_path)
    else:
        logger.error(f"{function_name()}: exposure #{exposure_number} was not saved; nothing to move")


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
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deepspec")

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
    def active_cameras(self) -> list[GreatEyes]:
        return [cam for cam in self.cameras.values() if cam is not None and cam.enabled and cam.detected]  # type: ignore

    @property
    def was_shut_down(self) -> bool:
        return all([cam.was_shut_down for cam in self.active_cameras])  # type: ignore

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        for cam in self.active_cameras:
            ret += cam.why_not_operational  # type: ignore
        return ret

    @property
    def operational(self) -> bool:
        for cam in self.active_cameras:
            if not cam.operational:  # type: ignore
                return False
        return True

    @property
    def name(self) -> str:
        return "deepspec"

    def startup(self):
        for cam in self.active_cameras:
            if cam:
                cam.startup()  # type: ignore # threads?

    def shutdown(self):
        for cam in self.active_cameras:
            if cam:
                cam.shutdown()  # type: ignore # threads?

        # Shutdown executor gracefully
        self.executor.shutdown(wait=False)

    @property
    def is_shutting_down(self) -> bool:
        return any([cam.is_shutting_down for cam in self.active_cameras])

    def powerdown(self):
        if not self.was_shut_down:
            logger.info("powerdown called without shutdown - calling shutdown first...")
            self.shutdown()
            time.sleep(3)  # let cameras start shutting down before we start waiting for them to finish

        while self.is_shutting_down:
            logger.info("waiting for cameras to finish shutting down before powering off...")
            time.sleep(0.5)

        for cam in self.active_cameras:
            cam.powerdown()  # type: ignore # threads?

        # Ensure executor is fully shutdown
        self.executor.shutdown(wait=True)

    def status(self) -> DeepspecStatus:
        if not any([cam.is_active(GreatEyesActivities.Acquiring) for cam in self.active_cameras]):  # type: ignore
            self.end_activity(DeepspecActivities.Acquiring)
            if self.spec is not None:
                self.spec.end_activity(SpecActivities.ExposingDeepspec)

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
            for cam in self.active_cameras:
                cam.abort()

    def start_acquisition(self, settings: SpecExposureSettings):
        self.start_activity(DeepspecActivities.Acquiring)
        if self.spec is not None:
            self.spec.start_activity(SpecActivities.ExposingDeepspec, data={"instrument": "deepspec"})
        self.expose(
            seconds=settings.exposure_duration,
            x_binning=settings.binning.x,  # type: ignore
            y_binning=settings.binning.y,  # type: ignore
            number_of_exposures=settings.number_of_exposures,
        )

    @property
    def is_working(self) -> bool:
        return self.is_active(DeepspecActivities.Acquiring)

    def expose(
        self,
        seconds: float,
        x_binning: int = 1,
        y_binning: int = 1,
        number_of_exposures: int | None = 1,
        frame_type: FrameType = FrameType.LIGHT,
        readout_amplifiers: ReadoutAmplifiersNames = "OSR_AND_OSL",
        readout_speed: ReadoutSpeedNames = "50_kHz",
        bypass_temperature_stabilization_check: bool = Query(
            default=False,
            description="Bypass the check for temperature stabilization (not recommended)",
        ),
        base_folder: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> CanonicalResponse:

        # Only a folder this call invented is this call's to release; when the caller
        # supplied one it belongs to that flow, exactly as in NewtonEMCCD.expose_single_image.
        owns_base_folder = base_folder is None
        if base_folder is None:
            base_folder = PathMaker().make_spec_exposures_folder(spec_name="deepspec")
            # make_spec_exposures_folder() ends in os.makedirs, so this directory is real
            # from here on. Claim it for the same reason its per-band children are claimed:
            # until it is reaped, another process's relocation sweep must see it as in use.
            MoveGuardian().claim_folder(base_folder)

        try:
            for cam in self.active_cameras:
                folder = str(Path(base_folder) / cam.band)  # type: ignore

                self.expose_one_camera(
                    band=cam.band,  # type: ignore
                    seconds=seconds,
                    x_binning=x_binning,
                    y_binning=y_binning,
                    number_of_exposures=number_of_exposures,
                    frame_type=frame_type,
                    readout_amplifiers=readout_amplifiers,
                    readout_speed=readout_speed,
                    bypass_temperature_stabilization_check=bypass_temperature_stabilization_check,
                    folder=folder,
                )
            return CanonicalResponse_Ok
        finally:
            # expose_one_camera released each band subfolder; nobody released their parent,
            # so every manual deepspec exposure left a `seq=NNN` directory behind on the ram
            # disk until the next startup sweep. The parent's reaper waits on the same
            # products the children do -- _folder_drained() looks at everything under the
            # folder -- so it cannot remove the tree while a band is still draining, and
            # _reap_folder tolerates children that were reaped first.
            if owns_base_folder:
                MoveGuardian().release_folder(base_folder, logger=logger)

    def expose_one_camera(
        self,
        band: DeepspecBands,
        seconds: float,
        x_binning: int = 1,
        y_binning: int = 1,
        delay_before_exposure: float = 0,
        number_of_exposures: int | None = 1,
        frame_type: FrameType = FrameType.LIGHT,
        readout_amplifiers: ReadoutAmplifiersNames = "OSR_AND_OSL",
        readout_speed: ReadoutSpeedNames = "50_kHz",
        bypass_temperature_stabilization_check: bool = Query(
            default=False,
            description="Bypass the check for temperature stabilization (not recommended)",
        ),
        folder: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> CanonicalResponse:
        future = self.executor.submit(
            self.do_expose_one_camera,
            band,
            seconds,
            x_binning,
            y_binning,
            delay_before_exposure,
            number_of_exposures,
            frame_type,
            readout_amplifiers,
            readout_speed=readout_speed,
            bypass_temperature_stabilization_check=bypass_temperature_stabilization_check,
            folder=folder,
        )
        time.sleep(0.5)  # give the thread a moment to start and potentially return an
        if future.done():
            if future.result() is not None:
                return future.result()
        return CanonicalResponse_Ok

    def do_expose_one_camera(
        self,
        band: DeepspecBands,
        seconds: float,
        x_binning: int = 1,
        y_binning: int = 1,
        delay_before_exposure: float = 0,
        number_of_exposures: int | None = 1,
        frame_type: FrameType = FrameType.LIGHT,
        readout_amplifiers: ReadoutAmplifiersNames = "OSR_AND_OSL",
        readout_speed: ReadoutSpeedNames = "50_kHz",
        bypass_temperature_stabilization_check: bool = False,
        folder: str | None = None,
    ) -> CanonicalResponse:

        if delay_before_exposure < 0:
            return CanonicalResponse(errors=[f"delay_before_exposure must be non-negative, got {delay_before_exposure}"])

        if not self.cameras[band] or not self.cameras[band].detected:  # type: ignore
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        camera = self.cameras[band]
        assert camera
        if not camera.detected:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        if not bypass_temperature_stabilization_check and camera.is_active(GreatEyesActivities.CoolingDown):
            return CanonicalResponse(
                errors=[f"camera '{band}' is still cooling down — use bypass_temperature_stabilization_check to override"]
            )

        if folder is None:
            folder = PathMaker().make_spec_exposures_folder(spec_name="deepspec", band=band)
        os.makedirs(folder, exist_ok=True)

        # `folder` is this function's alone -- it writes every exposure in it and nothing
        # else does -- so it is the one place that can release it, and every exit path
        # (including the early return on camera.errors) must reach the finally.
        try:
            if delay_before_exposure > 0:
                logger.info(f"delaying before exposure for {delay_before_exposure} seconds...")
                time.sleep(delay_before_exposure)

            readout: ReadoutModel = ReadoutModel(
                mode=ReadoutAmplifiersMapping[readout_amplifiers],
                speed=ReadoutSpeedMapping[readout_speed],
            )
            shutter: ShutterModel = ShutterModel(
                automatic=camera.conf.settings.shutter.automatic,
                close_time=camera.conf.settings.shutter.close_time,
                open_time=camera.conf.settings.shutter.open_time,
            )

            for exposure_number in range(number_of_exposures or 1):
                image_file = os.path.join(folder, "seq=" + PathMaker.make_seq(folder) + ".fits")

                settings: GreateyesSettingsModel = GreateyesSettingsModel(
                    # None, so start_exposure falls back to the configured value. This said
                    # 1, which the camera rejects -- `SetBitDepth(1)` returns "one or more
                    # parameters are out of range (8)", and greateyes' own BytesPerPixel
                    # enum only knows 2, 3 and 4. It survived because nothing in this path
                    # ever applied it: SetBitDepth lived in apply_settings, which the manual
                    # endpoint never called. This endpoint has no business inventing a
                    # sensor bit depth anyway.
                    bytes_per_pixel=None,
                    crop=None,
                    shutter=shutter,
                    exposure_duration=seconds,
                    number_of_exposures=number_of_exposures,
                    binning={"x": x_binning, "y": y_binning},  # type: ignore
                    image_file=image_file,
                    temp=None,
                    readout=readout,
                    probing=None,
                    frame_type=frame_type,
                )

                camera.start_exposure(
                    greateyes_exposure_settings=settings,
                    bypass_temperature_stabilization_check=bypass_temperature_stabilization_check,
                )

                # Whether to keep going is decided by what the camera is DOING, not by
                # whether it complained. An error that did not stop the exposure used to
                # return from here anyway -- and the `finally` below then reaped the folder
                # while the camera was still exposing. The frames landed ~25 s later in a
                # directory that no longer belonged to anyone, and nothing moved them to the
                # shared area. `Exposing` is the honest signal: start_exposure sets it only
                # once StartMeasurement has succeeded.
                errors = [f"exposure #{exposure_number} of {number_of_exposures}: '{e}'" for e in camera.errors]
                if not camera.is_active(GreatEyesActivities.Exposing):
                    if errors:
                        return CanonicalResponse(errors=errors)
                    logger.error(f"{function_name()}: exposure #{exposure_number} never started; nothing to wait for")
                    return CanonicalResponse(errors=[f"exposure #{exposure_number} of {number_of_exposures} never started"])

                # It IS exposing. Errors from here on are worth reporting, but this exposure
                # is going to produce a file, so see it through: wait, then hand it over.
                for e in errors:
                    logger.error(f"{function_name()}: {e}, continuing -- the exposure has started")

                while camera.is_active(GreatEyesActivities.Acquiring):
                    time.sleep(0.5)

                _hand_exposure_to_mover(camera, exposure_number)

            return CanonicalResponse_Ok
        finally:
            MoveGuardian().release_folder(folder, logger=logger)

    def adjust_temperature_one_camera(self, band: DeepspecBands, target_temperature: int | None = None):
        if band not in list(get_args(DeepspecBands)):
            return CanonicalResponse(errors=[f"invalid band '{band}', must be one of {list(get_args(DeepspecBands))}"])

        if not self.cameras[band]:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        camera = self.cameras[band]
        assert camera
        if not camera.detected:
            return CanonicalResponse(errors=[f"camera '{band}' not detected"])

        if target_temperature is None:
            return CanonicalResponse_Ok

        camera.adjust_temperature(target_temperature=target_temperature)

        if camera.errors:
            return CanonicalResponse(errors=[f"failed to adjust target temperature: '{e}'" for e in camera.errors])

        return CanonicalResponse_Ok

    def can_execute(self, assignment: SpectrographAssignment):
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

    def execute_assignment(self, remote_assignment: SpectrographAssignment, spec):
        assert isinstance(remote_assignment.spec, SpectrographModel)
        # deepspec_model: DeepspecModel = remote_assignment.spec

        while spec.is_moving:  # the fiber stage
            time.sleep(0.5)

        acquisition_folder = Path(PathMaker().make_spec_acquisitions_folder(spec_name="deepspec"))

        ulid = None
        if remote_assignment.batch is not None and remote_assignment.batch.ulid is not None:
            ulid = remote_assignment.batch.ulid
        elif remote_assignment.plan is not None and remote_assignment.plan.ulid is not None:
            ulid = remote_assignment.plan.ulid
        else:
            raise ValueError("assignment must have either batch.ulid or plan.ulid")

        Notifier().assignment_notification(
            AssignmentNotification(
                assignment_id=str(ulid),
                state="in-progress",
                # Relative to the shared root, not the absolute ram path this folder is
                # written to: the controller symlinks it, and a `D:` path means nothing
                # there. `move_ram_to_shared` only swaps ram.root for shared.root, so the
                # ram-relative path is exactly where these products land. MAST_spec#39.
                shared_top=os.path.relpath(acquisition_folder, Filer().ram.root),
                shared_subpath="deepspec",
            )
        )

        # From here on the ram-disk folder exists and the bands write into it, so every exit
        # path must reach release_folder() -- see the finally.
        try:
            self.start_activity(DeepspecActivities.Acquiring)
            spec.start_activity(SpecActivities.ExposingDeepspec, data={"instrument": "deepspec"})
            band_threads: list[threading.Thread] = []
            for band in list(self.cameras.keys()):
                camera = self.cameras[band]
                if not camera or not camera.detected:
                    continue

                band_threads.append(
                    camera.execute_assignment(
                        assignment=remote_assignment.assignment.spec,  # type: ignore
                        folder=str(acquisition_folder / band),
                    )
                )

            # Join the band threads rather than polling `cam.is_working`. That poll read
            # `is_active(Acquiring)`, a flag each band only sets once it reaches
            # start_exposure(), several SDK calls in. Starting four bands and immediately
            # polling could therefore find them all idle and fall through before any exposure
            # had begun, ending the activities early. Harmless-ish until now; with the
            # release below it would have reaped the folder from under the bands.
            for thread in band_threads:
                thread.join()

            self.end_activity(DeepspecActivities.Acquiring)
            spec.end_activity(SpecActivities.ExposingDeepspec)
        except Exception:
            logger.exception(f"{function_name()}: deepspec assignment failed")
        finally:
            # Reaped only once every protected product has reached the shared area, and
            # never before -- an exposure that failed to move keeps its folder rather than
            # being deleted with it.
            MoveGuardian().release_folder(str(acquisition_folder), logger=logger)

    @property
    def api_router(self) -> APIRouter:
        base_path = Const().BASE_SPEC_PATH + "/deepspec"
        tag = "Deepspec"
        router = APIRouter()

        router.add_api_route(base_path + "/status", tags=[tag], endpoint=self.status)
        router.add_api_route(base_path + "/startup", tags=[tag], endpoint=self.startup)
        router.add_api_route(base_path + "/shutdown", tags=[tag], endpoint=self.shutdown)
        router.add_api_route(base_path + "/abort", tags=[tag], endpoint=self.abort)
        router.add_api_route(base_path + "/expose", tags=[tag], endpoint=self.expose, response_model=None)
        router.add_api_route(
            base_path + "/expose_one_camera",
            tags=[tag],
            endpoint=self.expose_one_camera,
            response_model=None,
        )
        router.add_api_route(
            base_path + "/adjust_temperature_one_camera",
            tags=[tag],
            endpoint=self.adjust_temperature_one_camera,
            response_model=None,
        )

        return router


deepspec = Deepspec()
