from utils import PathMaker
import uuid


class Acquisition:

    def __init__(self):
        self.uuid = str(uuid.uuid1())
        self.dir = PathMaker().make_acquisition_folder_name(acquisition=self.uuid)
