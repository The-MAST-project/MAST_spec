import httpx
import logging
from utils import init_log
from typing import List


class DataLoggersInc:
    hostname: str
    user: str = 'admin'
    password: str = '1234'
    url_base: str
    socket_names: List[str]

    def __init__(self, hostname: str = None):
        self.logger = logging.getLogger('dli')
        init_log(self.logger)

        if hostname is None:
            self.logger.error(f"'hostname' parameter MUST be supplied")
            return

        self.hostname = hostname
        self.url_base = f"http://{self.user}:{self.password}@{self.hostname}/"
        self.socket_names = []  # TODO: get list from device
        self.timeout = 2

    def get(self, url: str):

        with httpx.Client as client:
            try:
                response = client.get(url, timeout=self.timeout)
                response.raise_for_status()
            except Exception as ex:
                self.logger.error(f"URL '{url} failed, ex={ex}")

        return response

    def name_to_socket_id(self, name: str) -> int | None:
        try:
            return self.socket_names.index(name)
        except Exception as ex:
            return None

    def is_on(self, socket_name: str):
        socket_id = self.name_to_socket_id(socket_name)
        if socket_id is None:
            return {'Error': f"No socket id for name '{socket_name}'"}

        response = self.get('status')
        if response.status_code == 200:
            # <!-- state=81 lock=10 -->
            # 0123456789012345678901234567890
            state = int(response.content[11:12], 16)
            lock = int(response.content[19:20], 16)

            return state & (1 << socket_id)

    def turn_on(self, socket_name: str):
        socket_id = self.name_to_socket_id(socket_name)
        if socket_id is None:
            return {'Error': f"No socket id for name '{socket_name}'"}

        self.get(f'outlet?{socket_id}=ON')

    def turn_off(self, socket_name: str):
        socket_id = self.name_to_socket_id(socket_name)
        if socket_id is None:
            return {'Error': f"No socket id for name '{socket_name}'"}

        self.get(f'outlet?{socket_id}=OFF')

    def cycle(self, socket_name:  str):
        socket_id = self.name_to_socket_id(socket_name)
        if socket_id is None:
            return {'Error': f"No socket id for name '{socket_name}'"}

        self.get(f'outlet?{socket_id}=CCL')

