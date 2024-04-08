from abc import ABC, abstractmethod
from enum import IntFlag
from threading import Timer, Lock
import logging
import platform
import os
import io

from config.config import Config
import datetime
from typing import List

default_log_level = logging.DEBUG
default_encoding = "utf-8"

BASE_SPEC_PATH = '/spec/'


class Timing:
    start_time: datetime.datetime
    end_time: datetime.datetime
    duration: datetime.timedelta

    def __init__(self):
        self.start_time = datetime.datetime.now()

    def end(self):
        self.end_time = datetime.datetime.now()
        self.duration = self.end_time - self.start_time


# class classproperty(property):
#     def __get__(self, obj, cls=None):
#         if cls is None:
#             cls = type(obj)
#         return super().__get__(cls)


class Activities:
    activities: IntFlag = 0
    timings: dict
    Idle = 0

    # @classproperty
    # def Idle(cls):
    #     return cls._idle

    def __init__(self):
        self.timings = dict()

    def start_activity(self, activity: IntFlag):
        self.activities |= activity
        self.timings[activity] = Timing()
        self.logger.info(f"started activity {activity.__repr__()}")

    def end_activity(self, activity: IntFlag):
        if not self.is_active(activity):
            return
        self.activities &= ~activity
        self.timings[activity].end()
        self.logger.info(f"ended activity {activity.__repr__()}, duration={self.timings[activity].duration}")

    def is_active(self, activity):
        return (self.activities & activity) != 0

    def is_idle(self):
        return self.activities == 0

    def __repr__(self):
        return self.activities.__repr__()


class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class SingletonFactory:
    _instances = {}
    _lock = Lock()

    @staticmethod
    def get_instance(class_type):
        with SingletonFactory._lock:
            if class_type not in SingletonFactory._instances:
                SingletonFactory._instances[class_type] = class_type()
        return SingletonFactory._instances[class_type]


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

    def __init__(self, path: str, mode='a', encoding=None, delay=True, errors=None):
        self.path = path
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        logging.FileHandler.__init__(self, filename='', delay=delay, mode=mode, encoding=encoding, errors=errors)


class PathMaker:
    top_folder: str

    def __init__(self):
        cfg = Config()
        self.top_folder = cfg.toml['global']['TopFolder']
        pass

    @staticmethod
    def make_seq(folder: str, camera: str | None = None) -> str:
        """
        Creates a sequence number by maintaining a '.seq' file.
        The sequence may be camera specific or camera agnostic.
        :param folder: Where to maintain the '.seq' file
        :param camera: What camera is the sequence for
        :return: The resulting sequence string
        """
        if camera:
            seq_file = os.path.join(folder, f'.{camera}.seq')
        else:
            seq_file = os.path.join(folder, '.seq')

        os.makedirs(os.path.dirname(seq_file), exist_ok=True)
        if os.path.exists(seq_file):
            with open(seq_file) as f:
                seq = int(f.readline())
        else:
            seq = 0
        seq += 1
        with open(seq_file, 'w') as file:
            file.write(f'{seq}\n')

        return f"{seq:04d}"

    def make_daily_folder_name(self):
        d = os.path.join(self.top_folder, datetime.datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(d, exist_ok=True)
        return d

    def make_exposure_file_name(self, camera: str, acquisition: str | None = None):
        if acquisition:
            folder = self.make_acquisition_folder_name(acquisition)
        else:
            folder = os.path.join(self.make_daily_folder_name(), 'Exposures')
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f'exposure-{camera}-{path_maker.make_seq(folder)}')

    def make_acquisition_folder_name(self, acquisition: str = None):
        acquisitions_folder = os.path.join(self.make_daily_folder_name(), 'Acquisitions')
        os.makedirs(acquisitions_folder, exist_ok=True)
        if acquisition is None:
            path = os.path.join(acquisitions_folder, f'acquisition-{PathMaker.make_seq(folder=acquisitions_folder)}')
        else:
            path = os.path.join(acquisitions_folder, f"{acquisition}")
        return path

    def make_guiding_folder_name(self):
        guiding_folder = os.path.join(self.make_daily_folder_name(), 'Guidings')
        os.makedirs(guiding_folder, exist_ok=True)
        return os.path.join(guiding_folder, f'guiding-{PathMaker.make_seq(guiding_folder)}')

    def make_logfile_name(self):
        daily_folder = os.path.join(self.make_daily_folder_name())
        os.makedirs(daily_folder)
        return os.path.join(daily_folder, 'log.txt')


path_maker = SingletonFactory.get_instance(PathMaker)


def init_log(logger: logging.Logger, level: int | None = None):
    logger.propagate = False
    level = default_log_level if level is None else level
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - {%(name)s:%(funcName)s:%(threadName)s:%(thread)s}' +
                                  ' -  %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # path_maker = SingletonFactory.get_instance(PathMaker)
    handler = DailyFileHandler(path=os.path.join(path_maker.make_daily_folder_name(), 'log.txt'), mode='a')
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# class CustomJSONEncoder(json.JSONEncoder):
#     def default(self, obj: Any) -> Any:
#         if isinstance(obj, float) and obj != obj:  # Check for NaN
#             return "NaN"  # or use None
#         elif isinstance(obj, datetime.datetime):
#             return obj.isoformat()  # Format datetime as ISO8601 string
#         # Add more custom handling cases here if needed
#         return super().default(obj)


# class CustomJSONResponse(JSONResponse):
#     media_type = "application/json"
#
#     def render(self, content: Any) -> bytes:
#         return json.dumps(
#             content,
#             ensure_ascii=False,
#             allow_nan=False,
#             indent=4,
#             separators=(", ", ": "),
#             cls=CustomJSONEncoder,
#         ).encode(default_encoding)


def deep_update(original: dict, update: dict):
    """
    Recursively update a dictionary with nested dictionaries.
    :param original: The original dictionary to be updated, in place.
    :param update: The dictionary with updates.
    """
    for key, value in update.items():
        if isinstance(value, dict) and key in original:
            # If the value is a dict and the key exists in the original dict,
            # perform a deep update
            deep_update(original[key], value)
        else:
            # Otherwise, update or add the key-value pair to the original dict
            original[key] = value


class Component(ABC, Activities):

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

    @property
    @abstractmethod
    def name(self) -> str:
        """The getter method for the abstract name property."""
        pass

    @name.setter
    @abstractmethod
    def name(self, value: str):
        """The setter method for the abstract name property."""
        pass

    @property
    @abstractmethod
    def operational(self) -> bool:
        """The getter method for the abstract name property."""
        pass

    @operational.setter
    @abstractmethod
    def operational(self, value: str) -> bool:
        """The setter method for the abstract name property."""
        pass

    @property
    @abstractmethod
    def why_not_operational(self) -> List[str]:
        pass
