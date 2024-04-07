import uvicorn
from fastapi import FastAPI
from config.config import Config
from networking import ping_peers
from spec import startup as spec_startup, shutdown as spec_shutdown, router as spec_router
from fastapi.responses import ORJSONResponse

from stage.stage import router as stage_router
from filter_wheel.wheel import router as filter_wheel_router
from cameras.andor.newton import router as highspec_camera_router
from cameras.greateyes.greateyes import router as deepspec_camera_router


app = FastAPI(default_response_class=ORJSONResponse)


@app.on_event("startup")
async def startup_event():
    spec_startup()


@app.on_event("shutdown")
async def shutdown_event():
    spec_shutdown()

# ping_peers(verbose=False)

app.include_router(stage_router)
app.include_router(filter_wheel_router)
app.include_router(highspec_camera_router)
app.include_router(deepspec_camera_router)
app.include_router(spec_router)


if __name__ == '__main__':
    cfg = Config()

    uvicorn_config = uvicorn.Config(app=app, host=cfg.toml['server']['host'], port=cfg.toml['server']['port'])
    uvicorn_server = uvicorn.Server(config=uvicorn_config)
    uvicorn_server.run()
