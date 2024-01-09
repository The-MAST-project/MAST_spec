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
import socket

default_log_level = logging.DEBUG


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
    _idle = Flag(0)

    @classproperty
    def Idle(cls):
        return cls._idle

    def __init__(self):
        self.activities = Activities.Idle

    def start_activity(self, activity: Flag):
        _only_one_bit_is_set(activity)
        self.activities |= activity
        self.timings[activity] = Timing()

    def end_activity(self, activity: Flag):
        _only_one_bit_is_set(activity)
        self.activities &= ~activity
        self.timings[activity].end()

    def is_active(self, activity):
        return activity in self.activities


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


# Singletons
path_maker = SingletonFactory.get_instance(PathMaker)


class ConfigTier:
    """
    Configuration tier.

    We use TOML files and TOMLDocuments to manage our configuration.
    The package we use is tomlkit (https://tomlkit.readthedocs.io/en/latest/) because:
    - TOML is a more rigorously defined .ini format
    - tomlkit supports keeping the order of the lines in the files AND comments.

    The actual configuration object (see Config below) uses three tiers which get merged into one configuration
    """
    mtime: float = None
    file: str | None = None
    defaults: TOMLDocument
    data: TOMLDocument

    def __init__(self, defaults: TOMLDocument = None, file=None):
        """

        Parameters
        ----------
        defaults
        file
        """
        self.file = file
        self.data = tomlkit.TOMLDocument()
        self.defaults = TOMLDocument()
        if defaults:
            self.defaults = defaults
            self.data = self.defaults

        if self.file and os.path.exists(self.file):
            self.load_file()
            self.mtime = os.path.getmtime(self.file)

    def load_file(self):
        if os.path.exists(self.file):
            with open(self.file, 'r') as f:
                file_values = tomlkit.load(f)
                self.data.clear()
                self.data.update(self.defaults)
                self.data.update(file_values)
                self.mtime = os.path.getmtime(self.file)

    def check_and_reload(self):
        current_mtime = os.path.getmtime(self.file) if os.path.exists(self.file) else None
        if current_mtime != self.mtime:
            self.load_file()


config_defaults = """
    [global]
        TopFolder = "C:/MAST"

    # stage positions
    [stage.grating]
        Ca = 1000
        H = 2000
        Mg = 3000
    
    [stage.camera]
        Ca = 1000
        H = 2000
        Mg = 3000
    
    [stage.fiber]
        DeepSpec = 1000
        HighSpec = 2000
        
    [fw.1]
        Pos1 = Empty
        Pos2 = ND1000
        Pos3 = ND2000
        Pos4 = ND3000
        Pos5 = ND4000
        Pos6 = ND5000
        Default = Pos1
        
    [fw.2]
        Pos1 = Empty
        Pos2 = ND1000
        Pos3 = ND2000
        Pos4 = ND3000
        Pos5 = ND4000
        Pos6 = ND5000
        Default = Pos1
"""


class Config:
    """
    Multi-tiered configuration for the MAST system.  It is based on a hierarchy ConfigTiers (see above)

    The tiers are merged in the following order:
    - first some hardcoded default values
    - next, global values loaded from the TopDir/config/mast.ini TOML file (if existent)
    - last (highest priority) host-specific values loaded from the TopDir/config/<hostname>.ini file (if existent)

    The configuration can be saved, the saved values go into the host-specific file.
    """
    data: TOMLDocument

    def __init__(self):
        self.default_config: ConfigTier = ConfigTier(defaults=tomlkit.parse(config_defaults))
        main_config_file = os.path.join('C:\\', 'MAST', 'config', 'spec.ini')  # cannot change
        self.global_config: ConfigTier = ConfigTier(file=main_config_file)

        top_folder = self.global_config.data['global']['TopFolder'] or os.path.join('C:\\', 'MAST')
        self.host_config: ConfigTier = ConfigTier(file=os.path.join(top_folder, 'config', socket.gethostname()))

        self.data = TOMLDocument()
        self.reload()

    def reload(self):
        self.data.clear()
        self.data.update(self.default_config.data)
        for tier in self.global_config, self.host_config:
            tier.check_and_reload()
            self.data.update(tier.data)

    def get(self, section: str, item: str):
        self.reload()
        if section in self.data:
            if item in self.data[section]:
                return self.data[section][item]
            else:
                raise KeyError(f"No item '{item} in section '{section}' in the configuration")
        else:
            raise KeyError(f"No section '{section} in the configuration")

    def set(self, section: str, item: str, value, comment=None):
        """
        Configuration changes are saved in the host-configuration tier

        Parameters
        ----------
        section
           The configuration section
        item
           The configuration item withing the specified section
        value
           The item's value
        comment
           Optional comment

        Returns
        -------

        """
        if section not in config.host_config.data:
            config.host_config.data[section] = tomlkit.table(True)
        self.host_config.data[section][item] = value
        if comment:
            self.host_config.data[section][item].comment(comment)

    def save(self):
        """
        TBD
        Returns
        -------

        """
        with open(self.host_config.file, 'w') as f:
            tomlkit.dump(self.host_config.data, f)


config: Config = SingletonFactory.get_instance(Config)


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
