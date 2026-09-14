import argparse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.config import Config
from common.endpoints import TIER_GROUPS, Tier, operation_area_tag
from common.filer import Filer
from common.mast_logging import configure_logging, get_logger
from cooling.chiller import Chiller
from deepspec import Deepspec
from filter_wheel.wheel import FilterWheels
from highspec import Highspec
from spec import Spec
from stage.stage import StageController

# Logging is configured once, here, before anything logs. Every 'mast.*' logger
# inherits the handlers and level from root by propagation.
# Precedence: --log-level > MAST_LOG_LEVEL > default.
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ... (overrides MAST_LOG_LEVEL)")
configure_logging(_parser.parse_known_args()[0].log_level)

logger = get_logger(__name__)

spec = Spec()


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    # Before anything is operational, so everything left on D: is by definition a leftover
    # from a previous run -- no live folder to race. Note this must sit here rather than
    # inside spec.startup(): that same method is exposed as an HTTP endpoint, and a later
    # /startup would re-trigger the sweep against folders that are in use.
    # MAST_common#52.
    Filer(logger).start_product_relocation_sweep(logger=logger)

    # Returns as soon as the startup is dispatched, so uvicorn begins serving while the
    # hardware is still coming up. /docs and /status are therefore reachable during a slow
    # or failing bring-up, which is when they are most wanted; SpecActivities.StartingUp
    # and `operational` say where it has got to.
    spec.startup()
    yield
    # Joins an in-flight startup first, bounded by Spec.startup_join_timeout_seconds.
    spec.shutdown()

    # Drain outstanding ram->shared moves while the process is still healthy, rather than
    # leaving them to be abandoned at interpreter teardown.
    Filer(logger).flush()


#: The operator areas in display order, each with its Swagger group description (#102).
#: An area is the path segment `common.endpoints.area_of` derives from where a route is mounted,
#: so an entry naming a segment no route produces renders an empty group -- and a new component
#: whose entry is missing renders last and undescribed. MAST_unit guards that with a test over
#: its component list; this repo has no test job to hang one on, because importing it commands
#: hardware (#77), so the list is kept in step by hand.
#:
#: The chiller has no entry on purpose: it serves the four generated lifecycle verbs and no
#: operator verb, so its routes are all in the interface group.
OPERATOR_AREAS: tuple[tuple[str, str], ...] = (
    (
        "spec",
        (
            "Operator verbs the spectrograph serves itself rather than delegating to one component: "
            "powering the instrument down, and the acquisition it runs on behalf of a unit. The "
            "filter-wheel listing is served here too, at the bare `/fw` path."
        ),
    ),
    (
        "deepspec",
        (
            "The four DeepSpec bands: a whole-instrument exposure, a single-camera exposure, and "
            "per-camera temperature adjustment."
        ),
    ),
    (
        "highspec",
        "HighSpec exposures and autofocus, and the Newton camera's cooldown and warmup.",
    ),
    (
        "stages",
        (
            "Zaber stage position and status, absolute and relative moves, and the fiber, disperser "
            "and focusing presets. Its startup / shutdown / abort sit here rather than in the "
            "interface group: `StageController` drives a set of stages and is not itself a "
            "`Component`, so its verbs take a stage name and carry no ABC guarantee."
        ),
    ),
    (
        "fw",
        (
            "Filter-wheel position, status and moves, and the same collection-level lifecycle verbs "
            "as the stages, for the same reason."
        ),
    ),
    (
        "simulate",
        (
            "Simulation of the light path and of the fiber, disperser and focus stages, for driving "
            "the instrument without moving it."
        ),
    ),
)

#: `openapi_tags` in display order: the contract surface first, then one group per operator area,
#: then the uniform lifecycle verbs. `TIER_GROUPS[Tier.OPERATION]` is unused -- every operator
#: route here files under an area -- and `Tier.DEMO` is left out because this repo serves no
#: parked route and a declared group with no members renders empty.
OPENAPI_TAGS: list[dict[str, str]] = [
    TIER_GROUPS[Tier.CONTRACT],
    *({"name": operation_area_tag(area), "description": description} for area, description in OPERATOR_AREAS),
    TIER_GROUPS[Tier.INTERFACE],
]


app = FastAPI(
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
    debug=True,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(spec.api_router)
app.include_router(Highspec(spec).api_router)
app.include_router(StageController(spec).api_router)
app.include_router(FilterWheels(spec).api_router)
app.include_router(Chiller().api_router)
app.include_router(Deepspec(spec).api_router)


@app.get("/favicon.ico")
def read_favicon():
    return RedirectResponse(url="/static/favicon.ico")


if __name__ == "__main__":
    server_conf = Config().get_service(service_name="spec")
    assert server_conf is not None
    uvicorn_config = uvicorn.Config(app=app, host=server_conf.listen_on, port=server_conf.port)

    uvicorn.Server(config=uvicorn_config).run()
