import uuid
import random
import json
import hashlib
from tempfile import NamedTemporaryFile

import pytest
import fakeredis
from unittest import mock
from typing import List

from assemblyline.common import forge, identify
from assemblyline.common.isotime import now_as_iso
from assemblyline.datastore.helper import AssemblylineDatastore
from assemblyline.datastore.stores.es_store import ESStore
from assemblyline.odm.models.config import Config
from assemblyline.odm.models.error import Error
from assemblyline.odm.models.result import Result
from assemblyline.odm.models.submission import Submission
from assemblyline.odm.messages.submission import Submission as SubmissionInput
from assemblyline.remote.datatypes.counters import MetricCounter
from assemblyline.remote.datatypes.queues.named import NamedQueue

from al_core.dispatching.client import DispatchClient
from al_core.dispatching.dispatcher import service_queue_name, ServiceTask
from al_core.submission_client import SubmissionClient
from al_core.ingester.ingester import IngestTask
from al_core.watcher import WatcherServer

from al_core.ingester.run_ingest import IngesterInput
from al_core.ingester.run_internal import IngesterInternals
from al_core.ingester.run_submit import IngesterSubmitter

from al_core.dispatching.run_files import FileDispatchServer
from al_core.dispatching.run_submissions import SubmissionDispatchServer

from al_core.server_base import ServerBase

from al_core.mocking import RedisTime



from al_core.dispatching.test_scheduler import dummy_service


@pytest.fixture(scope='module')
def redis():
    client = fakeredis.FakeStrictRedis()
    client.time = RedisTime()
    return client


@pytest.fixture(scope='module')
def es_connection():
    document_store = ESStore(['127.0.0.1'])
    if not document_store.ping():
        return pytest.skip("Connection to the Elasticsearch server failed. This test cannot be performed...")
    return AssemblylineDatastore(document_store)


class MockService(ServerBase):
    """Replaces everything past the dispatcher.

    Including service API, in the future probably include that in this test.
    """
    def __init__(self, name, datastore, redis, filestore):
        super().__init__('assemblyline.service.'+name)
        self.service_name = name
        self.queue = NamedQueue(service_queue_name(name), redis)
        self.dispatch_client = DispatchClient(datastore, redis)
        self.datastore = datastore
        self.filestore = filestore
        self.hits = dict()
        self.drops = dict()

    def try_run(self):
        while self.running:

            message = self.queue.pop(timeout=1)
            if not message:
                continue
            print(self.service_name, 'has received a job', message['sid'])

            task = ServiceTask(message)

            file = self.filestore.get(task.fileinfo.sha256)

            instructions = json.loads(file)
            instructions = instructions.get(self.service_name, {})
            print(self.service_name, 'following instruction:', instructions)
            hits = self.hits[task.fileinfo.sha256] = self.hits.get(task.fileinfo.sha256, 0) + 1

            if 'drop' in instructions:
                if instructions['drop'] >= hits:
                    self.drops[task.fileinfo.sha256] = self.drops.get(task.fileinfo.sha256, 0) + 1
                    continue

            if instructions.get('failure', False):
                error = None
                if 'error' in instructions:
                    error = Error(instructions['error'])
                self.dispatch_client.service_failed(task, error=error, error_key=uuid.uuid4().hex)
                continue

            result_data = {
                'classification': 'U',
                'response': {
                    'service_version': '0',
                    'service_name': self.service_name,
                },
                'result': {
                },
                'sha256': task.fileinfo.sha256
            }

            result_data.update(instructions.get('result', {}))
            result_data['response'].update(instructions.get('response', {}))

            result = Result(result_data)
            result_key = instructions.get('result_key', uuid.uuid4().hex)
            self.datastore.result.save(result_key, result)
            self.dispatch_client.service_finished(task, result, result_key)


@pytest.fixture(scope='module')
def replace_config(request):
    old_get_config = forge._get_config

    def replace():
        forge._get_config = old_get_config
    request.addfinalizer(replace)

    def new_config(*_, **__):
        config = old_get_config()
        config.core.dispatcher.timeout = 4
        config.core.dispatcher.debug_logging = True
        config.submission.max_extraction_depth = 5
        return config
    forge._get_config = new_config


class CoreSession:
    def __init__(self):
        self.ds: AssemblylineDatastore = None
        self.filestore = None
        self.config: Config = None


@pytest.fixture(scope='module')
@mock.patch('al_core.ingester.ingester.MetricCounter', new=mock.MagicMock(spec=MetricCounter))
@mock.patch('al_core.dispatching.dispatcher.MetricCounter', new=mock.MagicMock(spec=MetricCounter))
def core(request, es_connection, redis, replace_config):
    from assemblyline.common import log as al_log
    al_log.init_logging("simulation")

    fields = CoreSession()
    fields.redis = redis
    fields.ds = ds = es_connection
    fields.config = forge.get_config()
    threads = []
    fields.filestore = filestore = forge.get_filestore()
    threads: List[ServerBase] = [
        # Start the ingester components
        IngesterInput(datastore=ds, redis=redis, persistent_redis=redis),
        IngesterSubmitter(datastore=ds, redis=redis, persistent_redis=redis),
        IngesterInternals(datastore=ds, redis=redis, persistent_redis=redis),

        # Start the dispatcher
        FileDispatchServer(datastore=ds, redis=redis, redis_persist=redis),
        SubmissionDispatchServer(datastore=ds, redis=redis, redis_persist=redis),
        WatcherServer(redis=redis),
    ]

    ingester_input_thread: IngesterInput = threads[0]
    fields.ingest_queue = ingester_input_thread.ingester.ingest_queue

    ds.service.delete_matching('*')
    ds.service.save('pre', dummy_service('pre', 'EXTRACT'))
    threads.append(MockService('pre', ds, redis, filestore))
    fields.pre_service = threads[-1]
    ds.service.save('core-a', dummy_service('core-a', 'CORE'))
    threads.append(MockService('core-a', ds, redis, filestore))
    ds.service.save('core-b', dummy_service('core-b', 'CORE'))
    threads.append(MockService('core-b', ds, redis, filestore))
    ds.service.save('post', dummy_service('finish', 'POST'))
    threads.append(MockService('post', ds, redis, filestore))

    for t in threads:
        t.daemon = True
        t.start()

    def stop_core():
        [t.stop() for t in threads]
        [t.raising_join() for t in threads]
    request.addfinalizer(stop_core)
    return fields


def ready_body(core, body=None):
    out = {
        'salt': uuid.uuid4().hex,
    }
    out.update(body or {})
    out = json.dumps(out).encode()
    sha256 = hashlib.sha256()
    sha256.update(out)
    core.filestore.save(sha256.hexdigest(), out)

    with NamedTemporaryFile() as file:
        file.write(out)
        file.flush()
        fileinfo = identify.fileinfo(file.name)
        core.ds.save_or_freshen_file(sha256.hexdigest(), fileinfo, now_as_iso(500), 'U')

    return sha256.hexdigest(), len(out)


def ready_extract(core, children):
    if not isinstance(children, list):
        children = [children]

    body = {
        'pre': {
            'response': {
                'extracted': [{
                    'name': child,
                    'sha256': child,
                    'description': 'abc',
                    'classification': 'U'
                } for child in children]
            }
        }
    }
    return ready_body(core, body)


def test_deduplication(core):
    # -------------------------------------------------------------------------------
    # Submit two identical jobs, check that they get deduped by ingester
    sha, size = ready_body(core)

    for _ in range(2):
        core.ingest_queue.push(SubmissionInput(dict(
            metadata={},
            params=dict(
                services=dict(selected=''),
                submitter='user',
                groups=['user'],
            ),
            notification=dict(
                queue='1',
                threshold=0
            ),
            files=[dict(
                sha256=sha,
                size=size,
                name='abc123'
            )]
        )).as_primitives())

    notification_queue = NamedQueue('1', core.redis)
    first_task = notification_queue.pop(timeout=5)
    second_task = notification_queue.pop(timeout=5)

    # One of the submission will get processed fully
    first_task = IngestTask(first_task)
    first_submission: Submission = core.ds.submission.get(first_task.submission.sid)
    assert first_submission.state == 'completed'
    assert len(first_submission.files) == 1
    assert len(first_submission.errors) == 0
    assert len(first_submission.results) == 4

    # The other will get processed as a duplicate
    # (Which one is the 'real' one and which is the duplicate isn't important for our purposes)
    second_task = IngestTask(second_task)
    assert second_task.submission.sid == first_task.submission.sid

    # -------------------------------------------------------------------------------
    # Submit the same body, but change a parameter so the cache key misses,
    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=10000
        ),
        notification=dict(
            queue='2',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('2', core.redis)
    third_task = notification_queue.pop(timeout=5)

    # The third task should not be deduplicated by ingester, so will have a different submission
    third_task = IngestTask(third_task)
    third_submission: Submission = core.ds.submission.get(third_task.submission.sid)
    assert third_submission.state == 'completed'
    assert first_submission.sid != third_submission.sid
    assert len(third_submission.files) == 1
    assert len(third_submission.results) == 4


def test_watcher_recovery(core):
    # This time have the service server 'crash'
    sha, size = ready_body(core, {
        'pre': {'drop': 1}
    })

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=10000
        ),
        notification=dict(
            queue='drop',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('drop', core.redis)
    dropped_task = notification_queue.pop(timeout=16)
    assert dropped_task and IngestTask(dropped_task)
    assert core.pre_service.drops[sha] == 1
    assert core.pre_service.hits[sha] == 2


def test_dropping_early(core):
    # -------------------------------------------------------------------------------
    # This time have a file get marked for dropping by a service
    sha, size = ready_body(core, {
        'pre': {'result': {'drop_file': True}}
    })

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=10000
        ),
        notification=dict(
            queue='drop',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('drop', core.redis)
    dropped_task = IngestTask(notification_queue.pop(timeout=5))
    sub = core.ds.submission.get(dropped_task.submission.sid)
    assert len(sub.files) == 1
    assert len(sub.results) == 1


def test_service_error(core):
    # -------------------------------------------------------------------------------
    # Have a service produce an error
    # -------------------------------------------------------------------------------
    # This time have a file get marked for dropping by a service
    sha, size = ready_body(core, {
        'core-a': {
            'error': {
                'sha256': 'a'*64,
                'response': {
                    'message': 'words',
                    'status': 'FAIL_NONRECOVERABLE',
                    'service_name': 'core-a',
                    'service_version': '0'
                },
            },
            'failure': True,
        }
    })

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=10000
        ),
        notification=dict(
            queue='nq-error',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('nq-error', core.redis)
    task = IngestTask(notification_queue.pop(timeout=5))
    sub = core.ds.submission.get(task.submission.sid)
    assert len(sub.files) == 1
    assert len(sub.results) == 3
    assert len(sub.errors) == 1


def test_extracted_file(core):
    sha, size = ready_extract(core, ready_body(core)[0])

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=10000
        ),
        notification=dict(
            queue='nq-text-extracted-file',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('nq-text-extracted-file', core.redis)
    task = IngestTask(notification_queue.pop(timeout=5))
    sub = core.ds.submission.get(task.submission.sid)
    assert len(sub.files) == 1
    assert len(sub.results) == 8
    assert len(sub.errors) == 0


def test_depth_limit(core):
    # Make a nested set of files that goes deeper than the max depth
    sha, size = ready_body(core)
    for _ in range(core.config.submission.max_extraction_depth + 1):
        sha, size = ready_extract(core, sha)

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=core.config.submission.max_extraction_depth + 10
        ),
        notification=dict(
            queue='nq-test-depth-limit',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('nq-test-depth-limit', core.redis)
    task = IngestTask(notification_queue.pop(timeout=10))
    sub: Submission = core.ds.submission.get(task.submission.sid)
    assert len(sub.files) == 1
    # We should only get results for each file up to the max depth
    assert len(sub.results) == 4 * core.config.submission.max_extraction_depth
    assert len(sub.errors) == 0


def test_max_extracted_in_one(core):
    # Make a set of files that is bigger than max_extracted (3 in this case)
    children = [ready_body(core)[0] for _ in range(5)]
    sha, size = ready_extract(core, children)

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=3
        ),
        notification=dict(
            queue='nq-test-extracted-in-one',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('nq-test-extracted-in-one', core.redis)
    task = IngestTask(notification_queue.pop(timeout=10))
    sub: Submission = core.ds.submission.get(task.submission.sid)
    assert len(sub.files) == 1
    # We should only get results for each file up to the max depth
    assert len(sub.results) == 4 * (1 + 3)
    assert len(sub.errors) == 0


def test_max_extracted_in_several(core):
    # Make a set of in a non trivial tree, that add up to more than 3 (max_extracted) files
    children = [
        ready_extract(core, [ready_body(core)[0], ready_body(core)[0]])[0],
        ready_extract(core, [ready_body(core)[0], ready_body(core)[0]])[0]
    ]
    sha, size = ready_extract(core, children)

    core.ingest_queue.push(SubmissionInput(dict(
        metadata={},
        params=dict(
            services=dict(selected=''),
            submitter='user',
            groups=['user'],
            max_extracted=3
        ),
        notification=dict(
            queue='nq-test-extracted-in-several',
            threshold=0
        ),
        files=[dict(
            sha256=sha,
            size=size,
            name='abc123'
        )]
    )).as_primitives())

    notification_queue = NamedQueue('nq-test-extracted-in-several', core.redis)
    task = IngestTask(notification_queue.pop(timeout=10))
    sub: Submission = core.ds.submission.get(task.submission.sid)
    assert len(sub.files) == 1
    # We should only get results for each file up to the max depth
    assert len(sub.results) == 4 * (1 + 3)
    assert len(sub.errors) == 0