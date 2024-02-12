import uvicorn
from fastapi import FastAPI
from utils import Config

from stage.stage import router as stage_router

app = FastAPI()

app.include_router(stage_router)


if __name__ == '__main__':
    cfg = Config()

    uvicorn_config = uvicorn.Config(app=app, host=cfg.toml['server']['host'], port=cfg.toml['server']['port'])
    uvicorn_server = uvicorn.Server(config=uvicorn_config)
    uvicorn_server.run()
