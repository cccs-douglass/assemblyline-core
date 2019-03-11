import time

from assemblyline.remote.datatypes.queues.named import NamedQueue
from assemblyline.remote.datatypes.queues.priority import UniquePriorityQueue
from assemblyline.remote.datatypes.hash import ExpiringHash

from al_core.server_base import ServerBase
from al_core.watcher.client import WATCHER_HASH, WATCHER_QUEUE, MAX_TIMEOUT


class WatcherServer(ServerBase):
    def __init__(self, redis):
        super().__init__('assemblyline.watcher')
        self.redis = redis
        self.hash = ExpiringHash(name=WATCHER_HASH, ttl=MAX_TIMEOUT, host=redis)
        self.queue = UniquePriorityQueue(WATCHER_QUEUE, redis)

    def handle(self, message):
        try:
            queue = NamedQueue(message['queue'], self.redis)
            queue.push(message['message'])

        except Exception as error:
            self.log.error(error)

    def try_run(self):
        while self.running:
            seconds, _ = self.redis.time()
            messages = self.queue.dequeue_range(0, seconds)
            for key in messages:
                message = self.hash.pop(key)
                if message:
                    self.handle(message)
                else:
                    self.log.warning(f'Handled watch twice: {key} {len(key)} {type(key)}')

            if not messages:
                time.sleep(0.1)