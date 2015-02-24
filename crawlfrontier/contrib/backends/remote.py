"""This backend connects to an external crawl-frontier-server, 
which sends pages to be fetched via Kafka"""

import time

from kafka import KafkaClient, SimpleConsumer, SimpleProducer
from kafka.common import BrokerResponseError, KafkaUnavailableError

from scrapy.utils.serialize import ScrapyJSONEncoder, ScrapyJSONDecoder

from crawlfrontier import Backend
from crawlfrontier.core.models import Request


class TestManager(object):
    """To be able to run the backend without a real manager behind"""
    class Nothing(object):
        pass

    def __init__(self):
        def log(msg):
            print "Test Manager: ", msg

        self.logger = TestManager.Nothing()
        self.logger.backend = TestManager.Nothing()
        for log_level in (
                'info'
                'debug',
                'warning',
                'error'):
            setattr(self.logger.backend, log_level, log)
        
class KafkaBackend(Backend):
    DEFAULT_SERVER = "localhost:9092"
    DEFAULT_GROUP = "scrapy-crawler"
    DEFAULT_TOPIC_TODO = "frontier-todo"
    DEFAULT_TOPIC_DONE = "frontier-done"
    DEFAULT_WAIT_TIME = 1.0
    DEFAULT_COMM_TRIES = 5

    def __init__(self, 
                 manager=None,
                 server=None, 
                 group=None, 
                 topic_todo=None, 
                 topic_done=None, 
                 wait_time=None,
                 comm_tries=None):

        self._manager = manager or TestManager()
        self._seeds = []

        # Kafka connection parameters
        self._server = server or KafkaBackend.DEFAULT_SERVER
        self._topic_todo = topic_todo or KafkaBackend.DEFAULT_TOPIC_TODO
        self._topic_done = topic_done or KafkaBackend.DEFAULT_TOPIC_DONE
        self._group = group or KafkaBackend.DEFAULT_GROUP
        self._wait_time = wait_time or KafkaBackend.DEFAULT_WAIT_TIME
        self._comm_tries = comm_tries or KafkaBackend.DEFAULT_COMM_TRIES

        # Kafka setup
        try:
            self._conn = KafkaClient(self._server)
        except KafkaUnavailableError:
            self._manager.logger.backend.error(
                "Could not connect to Kafka server: " + self._server)
            raise

        self._prod = None
        self._cons = None

        self._connect_consumer()
        self._connect_producer()

        self._encoder = ScrapyJSONEncoder()
        self._decoder = ScrapyJSONDecoder()
                
    def _connect_producer(self):
        """If producer is not connected try to connect it now.

        :returns: bool -- True if producer is connected
        """        
        if self._prod is None:
            try:
                self._prod = SimpleProducer(self._conn)
            except BrokerResponseError:
                self._prod = None        
                if self._manager is not None:
                    self._manager.logger.backend.warning(
                        "Could not connect producer to Kafka server")
                return False

        return True

    def _connect_consumer(self):
        """If consumer is not connected try to connect it now.

        :returns: bool -- True if consumer is connected
        """
        if self._cons is None:
            try:
                self._cons = SimpleConsumer(self._conn, self._group, self._topic_todo)
            except BrokerResponseError:
                self._cons = None
                if self._manager is not None:
                    self._manager.logger.backend.warning(
                        "Could not connect consumer to Kafka server")
                return False

        return True

    @classmethod
    def from_manager(clas, manager):
        return KafkaBackend(
            manager=manager,
            server=manager.settings.get('KAFKA_SERVER'),
            group=manager.settings.get('KAFKA_GROUP'),
            topic_todo=manager.settings.get('KAFKA_TOPIC_TODO'),
            topic_done=manager.settings.get('KAFKA_TOPIC_DONE'),
        )

    def frontier_start(self):
        if self._connect_consumer():
            self._manager.logger.backend.info(
                "Successfully connected consumer to " + self._topic_todo)
        else:
            self._manager.logger.backend.warning(
                "Could not connect consumer to {0}. I will try latter.".format(
                    self._topic_todo))

    def frontier_stop(self):        
        # flush everything if a batch is incomplete
        self._prod.stop()

    def _send_message(self, obj):
        success = False
        if self._connect_producer():
            msg = self._encoder.encode(obj)
            n_tries = 0
            while not success and n_tries < self._comm_tries:
                try:
                    self._prod.send_messages(self._topic_done, msg)
                    success = True
                except BrokerResponseError:
                    n_tries += 1
                    if self._manager is not None:
                        self._manager.logger.backend.warning(
                            "Could not send message. Try {0}/{1}".format(
                                n_tries, self._comm_tries)
                        )

                    time.sleep(self._wait_time)

        return success

    def add_seeds(self, seeds):
        self._seeds += seeds

    def page_crawled(self, response, links):
        self._send_message({
            'url': response.url,
            'links': [link.url for link in links]
        })
            
    def request_error(self, page, error):
        pass

    def get_next_requests(self, max_n_requests):
        if self._seeds:
            n = min(len(self._seeds), max_n_requests)
            requests, self._seeds = self._seeds[:n], self._seeds[n:]
        else:
            if not self._connect_consumer():
                self._manager.logger.backend.warning(
                    "Could not connect consumer to " + self._topic_todo)
                return []

            urls = []           
            fails = 0
            success = False
            while not success and fails < self._comm_tries:
                for offmsg in self._cons.get_messages(
                        max_n_requests, 
                        timeout=self._wait_time):
                    success = True
                    try:
                        obj = self._decoder.decode(offmsg.message.value)            
                        try:
                            urls.append(obj['url'])
                        except (KeyError, TypeError):
                            self._manager.logger.backend.warning(
                                "Could not get url field in message")
                    except ValueError:
                        self._manager.logger.backend.warning(
                            "Could not decode {0} message: {1}".format(
                                self._topic_todo,
                                offmsg.message.value))
                if not success:
                    fails += 1
                    self._manager.logger.backend.warning(
                        "Timeout ({0} seconds) while trying to get {1} requests ({2}/{3} tries)".format(
                            self._wait_time,
                            max_n_requests,
                            fails,
                            self._comm_tries
                        )
                    )

            requests = map(Request, urls)

        return requests
