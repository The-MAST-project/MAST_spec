from config import Config, deep_search
import pythonping
import logging
from utils import init_log

logger = logging.getLogger('networking')
init_log(logger, logging.DEBUG)


class NetworkDestination:

    def __init__(self, address: str, port: int):
        self.address: str = address
        self.port: int = port


class NetworkedDevice:
    """
    A device accessed via an IP connection
    """

    def __init__(self, conf: dict):
        """

        :param conf: A dictionary with keys:
            - 'address' - [Mandatory] The IP address of the device
            - 'port'    - [Optional] Port
        """

        if 'network' not in conf:
            raise Exception(f"Missing 'network' key in {conf}")
        if 'address' not in conf['network']:
            raise Exception(f"Missing 'network.address' key in {conf}")

        self.ipaddress = conf['network']['address']
        self.port = int(conf['network']['port']) if 'port' in conf['network'] else None

        self.destination = NetworkDestination(address=self.ipaddress, port=self.port)


def ping_peers(verbose: bool = False):
    """
    ICMP pings all the configured peers
    :param verbose: If True, output results
    :return: Tuple(list of successes, list of failures)
    """
    conf = Config().toml
    failed = []
    succeeded = []

    print('Pinging network peers ...')
    responses = deep_search(conf, 'address')
    for response in responses:
        peer_name = response.path
        peer_name = peer_name.removesuffix('.network.address')

        response = pythonping.ping(response.value, timeout=2, count=1)
        if response.stats_success_ratio == 1.0:
            succeeded.append(peer_name)
            if verbose:
                logger.info(f"{peer_name} responds to ping")
        else:
            failed.append(peer_name)
            if verbose:
                logger.error(f"{peer_name} does not respond to ping")

    print(f"     responding: {succeeded}")
    print(f" not-responding: {failed}")


if __name__ == '__main__':
    ping_peers(verbose=False)
