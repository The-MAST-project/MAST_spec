import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from common.config import Config
from spec import startup as spec_startup, shutdown as spec_shutdown, router as spec_router
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from stage.stage import router as stage_router
from filter_wheel.wheel import router as filter_wheel_router
from cameras.andor.newton import router as highspec_camera_router
from cameras.greateyes.greateyes import router as deepspec_camera_router
from deepspec import router as deepspec_router
from cooling.chiller import router as chiller_router

@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    spec_startup()
    yield
    spec_shutdown()

app = FastAPI(
    docs_url='/docs',
    redocs_url=None,
    lifespan=lifespan,
    # openapi_url='/openapi.json',
    debug=True,
    default_response_class=ORJSONResponse,
    # exception_handlers={WebSocketDisconnect: websocket_disconnect_handler},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(spec_router)
app.include_router(highspec_camera_router)
app.include_router(deepspec_router)
app.include_router(stage_router)
app.include_router(filter_wheel_router)
app.include_router(chiller_router)

@app.get("/favicon.ico")
def read_favicon():
    return RedirectResponse(url="/static/favicon.ico")

if __name__ == '__main__':
    server_conf = Config().get_service(service_name='spec')
    uvicorn_config = uvicorn.Config(app=app, host=server_conf['listen_on'], port=server_conf['port'])

    uvicorn.Server(config=uvicorn_config).run()
