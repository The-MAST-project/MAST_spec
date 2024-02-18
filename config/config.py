import tomlkit
import os


class Config:
    file: str
    toml: tomlkit.TOMLDocument = None
    _instance = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.file = os.path.join(os.path.dirname(__file__), 'spec.toml')
        self.toml = tomlkit.TOMLDocument()
        self.reload()
        self._initialized = True

    def reload(self):
        self.toml.clear()
        with open(self.file, 'r') as f:
            self.toml = tomlkit.load(f)
