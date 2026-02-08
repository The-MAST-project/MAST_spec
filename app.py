from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.config import Config
from cooling.chiller import Chiller
from deepspec import Deepspec
from filter_wheel.wheel import FilterWheels
from highspec import Highspec
from spec import Spec
from stage.stage import StageController as StageController

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
    default_response_class=ORJSONResponse,
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
    uvicorn_config = uvicorn.Config(
        app=app, host=server_conf.listen_on, port=server_conf.port
    )

    uvicorn.Server(config=uvicorn_config).run()
