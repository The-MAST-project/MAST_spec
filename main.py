import uvicorn
from fastapi import FastAPI
from config.config import Config
from networking import ping_peers

from stage.stage import router as stage_router
from filter_wheel.wheel import router as filter_wheel_router
from cameras.andor.newton import router as highspec_camera_router

app = FastAPI()

responding, not_responding = ping_peers()
print(f"responding peers:     {responding}")
print(f"not-responding peers: {not_responding}")

app.include_router(stage_router)
app.include_router(filter_wheel_router)
app.include_router(highspec_camera_router)


if __name__ == '__main__':
    cfg = Config()

    uvicorn_config = uvicorn.Config(app=app, host=cfg.toml['server']['host'], port=cfg.toml['server']['port'])
    uvicorn_server = uvicorn.Server(config=uvicorn_config)
    uvicorn_server.run()
