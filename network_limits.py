"""Conservative shared media-download capacity for native fragment concurrency."""
from collections import deque
from contextlib import contextmanager
import threading

from settings import DEFAULTS, MAX_FRAGMENT_DOWNLOADS, MAX_NETWORK_CONNECTIONS


class NetworkLimits:
    def __init__(self):
        self.condition = threading.Condition()
        self.fragment_downloads = DEFAULTS["fragment_downloads"]
        self.network_connections = DEFAULTS["network_connections"]
        self.active_connections = 0
        self.waiters = deque()

    def configure(self, fragment_downloads, network_connections):
        if type(fragment_downloads) is not int or not 1 <= fragment_downloads <= MAX_FRAGMENT_DOWNLOADS:
            raise ValueError(f"Fragment downloads must be between 1 and {MAX_FRAGMENT_DOWNLOADS}")
        if type(network_connections) is not int or not 2 <= network_connections <= MAX_NETWORK_CONNECTIONS:
            raise ValueError(f"Media download budget must be between 2 and {MAX_NETWORK_CONNECTIONS}")
        with self.condition:
            self.fragment_downloads = fragment_downloads
            self.network_connections = network_connections
            self.condition.notify_all()

    @contextmanager
    def session(self, check_cancelled, on_wait, *, streams=1, fragmented=True):
        if type(streams) is not int or streams not in (1, 2):
            raise ValueError("A media session must reserve one or two streams")
        ticket = object()
        reserved = 0
        notified = False
        with self.condition:
            self.waiters.append(ticket)
        try:
            while True:
                check_cancelled()  # Includes pause waiting, outside the capacity lock.
                with self.condition:
                    available = self.network_connections - self.active_connections
                    if self.waiters[0] is ticket and available >= streams:
                        fragments = 1
                        if fragmented and self.fragment_downloads > 1 and available >= 3 * streams:
                            fragments = min(self.fragment_downloads, available // streams - 1)
                        # Reserve controller/key-request allowance alongside native workers.
                        reserved = streams * (fragments + 1 if fragments > 1 else 1)
                        self.active_connections += reserved
                        self.waiters.popleft()
                        self.condition.notify_all()
                        break
                if not notified:
                    on_wait()
                    notified = True
                with self.condition:
                    self.condition.wait(timeout=.1)
            check_cancelled()
            yield {"concurrent_fragment_downloads": fragments}
        finally:
            with self.condition:
                if reserved:
                    self.active_connections -= reserved
                else:
                    self.waiters.remove(ticket)
                self.condition.notify_all()
