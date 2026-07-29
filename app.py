import argparse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.config import Config
from cooling.chiller import Chiller
from deepspec import Deepspec
from filter_wheel.wheel import FilterWheels
from highspec import Highspec
from spec import Spec
from stage.stage import StageController as StageController
from common.mast_logging import configure_logging, get_logger

# Logging is configured once, here, before anything logs. Every 'mast.*' logger
# inherits the handlers and level from root by propagation.
# Precedence: --log-level > MAST_LOG_LEVEL > default.
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ... (overrides MAST_LOG_LEVEL)")
configure_logging(_parser.parse_known_args()[0].log_level)


spec = Spec()


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    spec.startup()
    yield
    spec.shutdown()


app = FastAPI(
    docs_url="/docs",
    redocs_url=None,
    lifespan=lifespan,
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
