import uvicorn
from fastapi import FastAPI
from utils import Config

from stage.stage import router as stage_router
from filter_wheel.wheel import router as filter_wheel_router

app = FastAPI()

app.include_router(stage_router)
app.include_router(filter_wheel_router)


if __name__ == '__main__':
    cfg = Config()

    uvicorn_config = uvicorn.Config(app=app, host=cfg.toml['server']['host'], port=cfg.toml['server']['port'])
    uvicorn_server = uvicorn.Server(config=uvicorn_config)
    uvicorn_server.run()
