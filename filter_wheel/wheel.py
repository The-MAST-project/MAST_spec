from utils import Component, Activities, RepeatTimer, init_log, config
from enum import Flag, auto
import logging


class WheelActivities(Flag):
    StartingUp = auto()
    ShuttingDown = auto()
    Moving = auto()
    GettingPosition = auto()


class Wheel(Component, Activities):
    id: int
    name: str
    position_names: list
    default_position: int
    target: int | None = None
    timer: RepeatTimer
    logger = None
    controller: None

    def __init__(self, wid: int):
        super().__init__()

        self.id = wid
        self.name = f"fw-{self.id}"

        self.logger = logging.getLogger(f"mast.spec.{self.name}")
        init_log(self.logger)

        self.position_names = list()
        for i in range(1, 7):
            self.position_names.append(config.get(section="fw.{self.id}", item="Pos{i}"))
        default_position_name = config.get(section="{self.name}", item="Default")
        self.default_position = int(default_position_name.replace("Pos", ""))

        self.controller = None  # get a handle to the controller

        self.timer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'camera-timer-thread'
        self.timer.start()

        self.logger.info('initialized')

    def startup(self):
        """
        Go to default position
        :return:
        """
        if self.position != self.default_position:
            self.start_activity(WheelActivities.StartingUp)
            self.move(self.default_position)

    def shutdown(self):
        """
        Return to default position
        :return:
        """
        if self.position != self.default_position:
            self.start_activity(WheelActivities.ShuttingDown)
            self.move(self.default_position)

    def abort(self):
        # TBD: tell the controller to stop moving (if possible)
        if self.is_active(WheelActivities.Moving):
            self.end_activity(WheelActivities.Moving)

    def status(self):
        pass

    @property
    def position(self) -> int:
        """
        Get the current position from the controller
        :return:
        """
        self.start_activity(WheelActivities.GettingPosition)
        pos = 0  # TBD: get it from the controller
        self.end_activity(WheelActivities.GettingPosition)
        return pos

    def move(self, pos: int | str):
        if type(pos) is str:
            pos = self.name_to_number(pos)
        self.start_activity(WheelActivities.Moving)
        self.target = pos
        # TBD: tell the controller to move to target position

    def name_to_number(self, pos_name: str) -> int | None:
        try:
            idx = self.position_names.index(pos_name)
        except ValueError:
            raise Exception(f"Bad position name '{pos_name}'.  Known position names: {self.position_names}")

        return idx

    def ontimer(self):
        if self.is_active(WheelActivities.Moving) and not self.controller.is_moving:
            self.end_activity(WheelActivities.Moving)
            if self.is_active(WheelActivities.StartingUp):
                self.end_activity(WheelActivities.StartingUp)
            if self.is_active(WheelActivities.ShuttingDown):
                self.end_activity(WheelActivities.ShuttingDown)
