import datetime
from abc import ABC, abstractmethod
from enum import Flag
from threading import Timer, Lock
import logging
import platform
import os
import io
from tomlkit import TOMLDocument
import tomlkit
import json
from starlette.responses import Response
from typing import Any

default_log_level = logging.DEBUG
default_encoding = "utf-8"

BASE_SPEC_PATH = '/spec/'


class Component(ABC):

    @abstractmethod
    def startup(self):
        """
        Called whenever an observing session starts (at sun-down or when safety returns)
        :return:
        """
        pass

    @abstractmethod
    def shutdown(self):
        """
        Called whenever an observing session is terminated (at sun-up or when becoming unsafe)
        :return:
        """
        pass

    @abstractmethod
    def abort(self):
        """
        Immediately terminates any in-progress activities and returns the component to its
         default state.
        :return:
        """
        pass

    @abstractmethod
    def status(self):
        """
        Returns the component's current status
        :return:
        """
        pass


class Timing:
    start_time: datetime.datetime
    end_time: datetime.datetime
    duration: datetime.timedelta

    def __init__(self):
        self.start_time = datetime.datetime.now()

    def end(self):
        self.end_time = datetime.datetime.now()
        self.duration = self.end_time - self.start_time


class classproperty(property):
    def __get__(self, obj, cls=None):
        if cls is None:
            cls = type(obj)
        return super().__get__(cls)


class Activities:
    activities: Flag
    timings: dict
    Idle = 0

    # @classproperty
    # def Idle(cls):
    #     return cls._idle

    def __init__(self):
        self.activities = Activities.Idle
        self.timings = dict()

    def start_activity(self, activity: Flag):
        _only_one_bit_is_set(activity.value)
        self.activities |= activity.value
        self.timings[activity] = Timing()
        self.logger.info(f"started activity {activity}")

    def end_activity(self, activity: Flag):
        _only_one_bit_is_set(activity.value)
        self.activities &= ~activity.value
        self.timings[activity].end()
        self.logger.info(f"ended activity {activity}, duration={self.timings[activity].duration}")

    def is_active(self, activity):
        return (self.activities & activity.value) != 0

    def is_idle(self):
        return self.activities == Activities.Idle


class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


def _only_one_bit_is_set(f: Flag):
    n = int(f)
    if n > 0 and (n & (n - 1)) == 0:
        return True
    raise Exception(f"More than one bit is set in 0x{n:x}")


class SingletonFactory:
    _instances = {}
    _lock = Lock()

    @staticmethod
    def get_instance(class_type):
        with SingletonFactory._lock:
            if class_type not in SingletonFactory._instances:
                SingletonFactory._instances[class_type] = class_type()
        return SingletonFactory._instances[class_type]


class Config:
    file: str
    toml: TOMLDocument = None
    _instance = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.file = os.path.join('C:\\Users\\User\\PycharmProjects\\MAST_spec', 'config', 'spec.toml')
        self.toml = TOMLDocument()
        self.reload()
        self._initialized = True

    def reload(self):
        self.toml.clear()
        with open(self.file, 'r') as f:
            self.toml = tomlkit.load(f)

    def get(self, section: str, item: str | None = None):
        self.reload()

        if item is None:
            return self.toml[section] if section in self.toml else None

        if section in self.toml:
            if item in self.toml[section]:
                return self.toml[section][item]
            else:
                raise KeyError(f"No item '{item} in section '{section}' in the configuration")
        else:
            raise KeyError(f"No section '{section} in the configuration")

    # def set(self, section: str, item: str, value, comment=None):
    #     """
    #     Configuration changes are saved in the host-configuration tier
    #
    #     Parameters
    #     ----------
    #     section
    #        The configuration section
    #     item
    #        The configuration item withing the specified section
    #     value
    #        The item's value
    #     comment
    #        Optional comment
    #
    #     Returns
    #     -------
    #
    #     """
    #     if section not in config.host_config.toml:
    #         config.host_config.toml[section] = tomlkit.table(True)
    #     self.host_config.toml[section][item] = value
    #     if comment:
    #         self.host_config.toml[section][item].comment(comment)
    #
    # def save(self):
    #     """
    #     TBD
    #     Returns
    #     -------
    #
    #     """
    #     with open(self.host_config.file, 'w') as f:
    #         tomlkit.dump(self.host_config.toml, f)




class DailyFileHandler(logging.FileHandler):

    filename: str = ''
    path: str

    def make_file_name(self):
        """
        Produces file names for the DailyFileHandler, which rotates them daily at noon (UT).
        The filename has the format <top><daily><bottom> and includes:
        * A top section (either /var/log/mast on Linux or %LOCALAPPDATA%/mast on Windows
        * The daily section (current date as %Y-%m-%d)
        * The bottom path, supplied by the user
        Examples:
        * /var/log/mast/2022-02-17/server/app.log
        * c:\\User\\User\\LocalAppData\\mast\\2022-02-17\\main.log
        :return:
        """
        top = ''
        if platform.platform() == 'Linux':
            top = '/var/log/mast'
        elif platform.platform().startswith('Windows'):
            top = os.path.join(os.path.expandvars('%LOCALAPPDATA%'), 'mast')
        now = datetime.datetime.now()
        if now.hour < 12:
            now = now - datetime.timedelta(days=1)
        return os.path.join(top, f'{now:%Y-%m-%d}', self.path)

    def emit(self, record: logging.LogRecord):
        """
        Overrides the logging.FileHandler's emit method.  It is called every time a log record is to be emitted.
        This function checks whether the handler's filename includes the current date segment.
        If not:
        * A new file name is produced
        * The handler's stream is closed
        * A new stream is opened for the new file
        The record is emitted.
        :param record:
        :return:
        """
        filename = self.make_file_name()
        if not filename == self.filename:
            if self.stream is not None:
                # we have an open file handle, clean it up
                self.stream.flush()
                self.stream.close()
                self.stream = None  # See Issue #21742: _open () might fail.

            self.baseFilename = filename
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
            self.stream = self._open()
        logging.StreamHandler.emit(self, record=record)

    def __init__(self, path: str, mode='a', encoding=None, delay=False, errors=None):
        self.path = path
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        logging.FileHandler.__init__(self, filename='', delay=True, mode=mode, encoding=encoding, errors=errors)


config: Config = Config()


class PathMaker:
    top_folder: str

    def __init__(self):
        self.top_folder = config.get('global', 'TopFolder')
        pass

    @staticmethod
    def make_seq(path: str):
        seq_file = os.path.join(path, '.seq')

        os.makedirs(os.path.dirname(seq_file), exist_ok=True)
        if os.path.exists(seq_file):
            with open(seq_file) as f:
                seq = int(f.readline())
        else:
            seq = 0
        seq += 1
        with open(seq_file, 'w') as file:
            file.write(f'{seq}\n')

        return seq

    def make_daily_folder_name(self):
        d = os.path.join(self.top_folder, datetime.datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(d, exist_ok=True)
        return d

    def make_exposure_file_name(self):
        exposures_folder = os.path.join(self.make_daily_folder_name(), 'Exposures')
        os.makedirs(exposures_folder, exist_ok=True)
        return os.path.join(exposures_folder, f'exposure-{path_maker.make_seq(exposures_folder):04d}')

    def make_acquisition_folder_name(self):
        acquisitions_folder = os.path.join(self.make_daily_folder_name(), 'Acquisitions')
        os.makedirs(acquisitions_folder, exist_ok=True)
        return os.path.join(acquisitions_folder, f'acquisition-{PathMaker.make_seq(acquisitions_folder)}')

    def make_guiding_folder_name(self):
        guiding_folder = os.path.join(self.make_daily_folder_name(), 'Guidings')
        os.makedirs(guiding_folder, exist_ok=True)
        return os.path.join(guiding_folder, f'guiding-{PathMaker.make_seq(guiding_folder)}')

    def make_logfile_name(self):
        daily_folder = os.path.join(self.make_daily_folder_name())
        os.makedirs(daily_folder)
        return os.path.join(daily_folder, 'log.txt')


path_maker = SingletonFactory.get_instance(PathMaker)


def init_log(logger: logging.Logger):
    logger.propagate = False
    logger.setLevel(default_log_level)
    handler = logging.StreamHandler()
    handler.setLevel(default_log_level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - {%(name)s:%(funcName)s:%(threadName)s:%(thread)s}' +
                                  ' -  %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # path_maker = SingletonFactory.get_instance(PathMaker)
    handler = DailyFileHandler(path=os.path.join(path_maker.make_daily_folder_name(), 'log.txt'), mode='a')
    handler.setLevel(default_log_level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class PrettyJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=4,
            separators=(", ", ": "),
        ).encode(default_encoding)
