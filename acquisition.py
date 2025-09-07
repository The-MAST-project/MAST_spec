import uuid

from common.utils import PathMaker


class Acquisition:
    def __init__(self):
        self.uuid = str(uuid.uuid1())
        self.dir = PathMaker().make_acquisition_folder()
