import errno
import logging
import socket
from collections import deque
from unittest.mock import MagicMock, Mock, call, patch

import pytest
from amqp import ChannelError
from billiard.exceptions import RestartFreqExceeded

from celery import bootsteps
from celery.contrib.testing.mocks import ContextMock
from celery.exceptions import WorkerShutdown, WorkerTerminate
from celery.utils.collections import LimitedSet
from celery.utils.quorum_queues import detect_quorum_queues
from celery.worker.consumer.agent import Agent
from celery.worker.consumer.consumer import CANCEL_TASKS_BY_DEFAULT, CLOSE, TERMINATE, Consumer
from celery.worker.consumer.gossip import Gossip
from celery.worker.consumer.heart import Heart
from celery.worker.consumer.mingle import Mingle
from celery.worker.consumer.tasks import Tasks
from celery.worker.state import active_requests


class ConsumerTestCase:
    def get_consumer(self, no_hub=False, **kwargs):
        consumer = Consumer(
            on_task_request=Mock(),
            init_callback=Mock(),
            pool=Mock(),
            app=self.app,
            timer=Mock(),
            controller=Mock(),
            hub=None if no_hub else Mock(),
            **kwargs
        )
        consumer.blueprint = Mock(name='blueprint')
        consumer.pool.num_processes = 2
        consumer._restart_state = Mock(name='_restart_state')
        consumer.connection = _amqp_connection()
        consumer.connection_errors = (socket.error, OSError,)
        consumer.conninfo = consumer.connection
        return consumer


class test_Consumer(ConsumerTestCase):
    def setup_method(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y

        self.add = add

    def test_repr(self):
        assert repr(self.get_consumer())

    def test_taskbuckets_defaultdict(self):
        c = self.get_consumer()
        assert c.task_buckets['fooxasdwx.wewe'] is None

    def test_sets_heartbeat(self):
        c = self.get_consumer(amqheartbeat=10)
        assert c.amqheartbeat == 10
        self.app.conf.broker_heartbeat = 20
        c = self.get_consumer(amqheartbeat=None)
        assert c.amqheartbeat == 20

    def test_gevent_bug_disables_connection_timeout(self):
        with patch('celery.worker.consumer.consumer._detect_environment') as d:
            d.return_value = 'gevent'
            self.app.conf.broker_connection_timeout = 33.33
            self.get_consumer()
            assert self.app.conf.broker_connection_timeout is None

    def test_limit_moved_to_pool(self):
        with patch('celery.worker.consumer.consumer.task_reserved') as task_reserved:
            c = self.get_consumer()
            c.on_task_request = Mock(name='on_task_request')
            request = Mock(name='request')
            c._limit_move_to_pool(request)
            task_reserved.assert_called_with(request)
            c.on_task_request.assert_called_with(request)

    def test_update_prefetch_count(self):
        c = self.get_consumer()
        c._update_qos_eventually = Mock(name='update_qos')
        c.initial_prefetch_count = None
        c.pool.num_processes = None
        c.prefetch_multiplier = 10
        assert c._update_prefetch_count(1) is None
        c.initial_prefetch_count = 10
        c.pool.num_processes = 10
        c._update_prefetch_count(8)
        c._update_qos_eventually.assert_called_with(8)
        assert c.initial_prefetch_count == 10 * 10

    @pytest.mark.parametrize(
        'active_requests_count,expected_initial,expected_maximum,enabled',
        [
            [0, 2, True, True],
            [1, 1, False, True],
            [2, 1, False, True],
            [0, 2, True, False],
            [1, 2, True, False],
            [2, 2, True, False],
        ]
    )
    @patch('celery.worker.consumer.consumer.active_requests', new_callable=set)
    def test_restore_prefetch_count_on_restart(self, active_requests_mock, active_requests_count,
                                               expected_initial, expected_maximum, enabled, subtests):
        self.app.conf.worker_enable_prefetch_count_reduction = enabled

        reqs = {Mock() for _ in range(active_requests_count)}
        active_requests_mock.update(reqs)

        c = self.get_consumer()
        c.qos = Mock()
        c.blueprint = Mock()

        def bp_start(*_, **__):
            if c.restart_count > 1:
                c.blueprint.state = CLOSE
            else:
                raise ConnectionError

        c.blueprint.start.side_effect = bp_start

        c.start()

        with subtests.test("initial prefetch count is never 0"):
            assert c.initial_prefetch_count != 0

        with subtests.test(f"initial prefetch count is equal to {expected_initial}"):
            assert c.initial_prefetch_count == expected_initial

        with subtests.test("maximum prefetch is reached"):
            assert c._maximum_prefetch_restored is expected_maximum

    def test_restore_prefetch_count_after_connection_restart_negative(self):
        self.app.conf.worker_enable_prefetch_count_reduction = False

        c = self.get_consumer()
        c.qos = Mock()

        # Overcome TypeError: 'Mock' object does not support the context manager protocol
        class MutexMock:
            def __enter__(self):
                pass

            def __exit__(self, *args):
                pass

        c.qos._mutex = MutexMock()

        assert c._restore_prefetch_count_after_connection_restart(None) is None

    def test_create_task_handler(self, subtests):
        c = self.get_consumer()
        c.qos = MagicMock()
        c.qos.value = 1
        c._maximum_prefetch_restored = False

        sig = self.add.s(2, 2)
        message = self.task_message_from_sig(self.app, sig)

        def raise_exception():
            raise KeyError('Foo')

        def strategy(_, __, ack_log_error_promise, ___, ____):
            ack_log_error_promise()

        c.strategies[sig.task] = strategy
        c.call_soon = raise_exception
        on_task_received = c.create_task_handler()
        on_task_received(message)

        with subtests.test("initial prefetch count is never 0"):
            assert c.initial_prefetch_count != 0

        with subtests.test("initial prefetch count is 2"):
            assert c.initial_prefetch_count == 2

        with subtests.test("maximum prefetch is reached"):
            assert c._maximum_prefetch_restored is True

    def test_flush_events(self):
        c = self.get_consumer()
        c.event_dispatcher = None
        c._flush_events()
        c.event_dispatcher = Mock(name='evd')
        c._flush_events()
        c.event_dispatcher.flush.assert_called_with()

    def test_on_send_event_buffered(self):
        c = self.get_consumer()
        c.hub = None
        c.on_send_event_buffered()
        c.hub = Mock(name='hub')
        c.on_send_event_buffered()
        c.hub._ready.add.assert_called_with(c._flush_events)

    def test_schedule_bucket_request(self):
        c = self.get_consumer()
        c.timer = Mock()

        bucket = Mock()
        request = Mock()
        bucket.pop = lambda: bucket.contents.popleft()
        bucket.can_consume.return_value = True
        bucket.contents = deque()

        with patch(
            'celery.worker.consumer.consumer.Consumer._limit_move_to_pool'
        ) as task_reserved:
            bucket.contents.append((request, 3))
            c._schedule_bucket_request(bucket)
            bucket.can_consume.assert_called_with(3)
            task_reserved.assert_called_with(request)

        bucket.can_consume.return_value = False
        bucket.contents = deque()
        bucket.expected_time.return_value = 3.33
        bucket.contents.append((request, 4))
        limit_order = c._limit_order
        c._schedule_bucket_request(bucket)
        assert c._limit_order == limit_order + 1
        bucket.can_consume.assert_called_with(4)
        c.timer.call_after.assert_called_with(
            3.33, c._schedule_bucket_request, (bucket,),
            priority=c._limit_order,
        )
        bucket.expected_time.assert_called_with(4)
        assert bucket.pop() == (request, 4)

        bucket.contents = deque()
        bucket.can_consume.reset_mock()
        c._schedule_bucket_request(bucket)
        bucket.can_consume.assert_not_called()

    def test_limit_task(self):
        c = self.get_consumer()
        bucket = Mock()
        request = Mock()

        with patch(
            'celery.worker.consumer.consumer.Consumer._schedule_bucket_request'
        ) as task_reserved:
            c._limit_task(request, bucket, 1)
            bucket.add.assert_called_with((request, 1))
            task_reserved.assert_called_with(bucket)

    def test_post_eta(self):
        c = self.get_consumer()
        c.qos = Mock()
        bucket = Mock()
        request = Mock()

        with patch(
            'celery.worker.consumer.consumer.Consumer._schedule_bucket_request'
        ) as task_reserved:
            c._limit_post_eta(request, bucket, 1)
            c.qos.decrement_eventually.assert_called_with()
            bucket.add.assert_called_with((request, 1))
            task_reserved.assert_called_with(bucket)

    def test_max_restarts_exceeded(self):
        c = self.get_consumer()

        def se(*args, **kwargs):
            c.blueprint.state = CLOSE
            raise RestartFreqExceeded()

        c._restart_state.step.side_effect = se
        c.blueprint.start.side_effect = socket.error()

        with patch('celery.worker.consumer.consumer.sleep') as sleep:
            c.start()
            sleep.assert_called_with(1)

    def test_do_not_restart_when_closed(self):
        c = self.get_consumer()

        c.blueprint.state = None

        def bp_start(*args, **kwargs):
            c.blueprint.state = CLOSE

        c.blueprint.start.side_effect = bp_start
        with patch('celery.worker.consumer.consumer.sleep'):
            c.start()

        c.blueprint.start.assert_called_once_with(c)

    def test_do_not_restart_when_terminated(self):
        c = self.get_consumer()

        c.blueprint.state = None

        def bp_start(*args, **kwargs):
            c.blueprint.state = TERMINATE

        c.blueprint.start.side_effect = bp_start

        with patch('celery.worker.consumer.consumer.sleep'):
            c.start()

        c.blueprint.start.assert_called_once_with(c)

    def test_too_many_open_files_raises_error(self):
        c = self.get_consumer()
        err = OSError()
        err.errno = errno.EMFILE
        c.blueprint.start.side_effect = err
        with pytest.raises(WorkerTerminate):
            c.start()

    def _closer(self, c):
        def se(*args, **kwargs):
            c.blueprint.state = CLOSE

        return se

    @pytest.mark.parametrize("broker_connection_retry", [True, False])
    def test_blueprint_restart_when_state_not_in_stop_conditions(self, broker_connection_retry):
        c = self.get_consumer()

        # ensure that WorkerShutdown is not raised
        c.app.conf['broker_connection_retry'] = broker_connection_retry
        c.app.conf['broker_connection_retry_on_startup'] = True
        c.restart_count = -1

        # ensure that blueprint state is not in stop conditions
        c.blueprint.state = bootsteps.RUN
        c.blueprint.start.side_effect = ConnectionError()

        # stops test from running indefinitely in the while loop
        c.blueprint.restart.side_effect = self._closer(c)

        c.start()
        c.blueprint.restart.assert_called_once()

    @pytest.mark.parametrize("broker_channel_error_retry", [True, False])
    def test_blueprint_restart_for_channel_errors(self, broker_channel_error_retry):
        c = self.get_consumer()

        # ensure that WorkerShutdown is not raised
        c.app.conf['broker_connection_retry'] = True
        c.app.conf['broker_connection_retry_on_startup'] = True
        c.app.conf['broker_channel_error_retry'] = broker_channel_error_retry
        c.restart_count = -1

        # ensure that blueprint state is not in stop conditions
        c.blueprint.state = bootsteps.RUN
        c.blueprint.start.side_effect = ChannelError()

        # stops test from running indefinitely in the while loop
        c.blueprint.restart.side_effect = self._closer(c)

        # restarted only when broker_channel_error_retry is True
        if broker_channel_error_retry:
            c.start()
            c.blueprint.restart.assert_called_once()
        else:
            with pytest.raises(ChannelError):
                c.start()

    def test_collects_at_restart(self):
        c = self.get_consumer()
        c.connection.collect.side_effect = MemoryError()
        c.blueprint.start.side_effect = socket.error()
        c.blueprint.restart.side_effect = self._closer(c)
        c.start()
        c.connection.collect.assert_called_with()

    def test_register_with_event_loop(self):
        c = self.get_consumer()
        c.register_with_event_loop(Mock(name='loop'))

    def test_on_close_clears_semaphore_timer_and_reqs(self):
        with patch('celery.worker.consumer.consumer.reserved_requests') as res:
            c = self.get_consumer()
            c.on_close()
            c.controller.semaphore.clear.assert_called_with()
            c.timer.clear.assert_called_with()
            res.clear.assert_called_with()
            c.pool.flush.assert_called_with()

            c.controller = None
            c.timer = None
            c.pool = None
            c.on_close()

    def test_connect_error_handler(self):
        self.app._connection = _amqp_connection()
        conn = self.app._connection.return_value
        c = self.get_consumer()
        assert c.connect()
        conn.ensure_connection.assert_called()
        errback = conn.ensure_connection.call_args[0][0]
        errback(Mock(), 0)

    @patch('celery.worker.consumer.consumer.error')
    def test_connect_error_handler_progress(self, error):
        self.app.conf.broker_connection_retry = True
        self.app.conf.broker_connection_max_retries = 3
        self.app._connection = _amqp_connection()
        conn = self.app._connection.return_value
        c = self.get_consumer()
        assert c.connect()
        errback = conn.ensure_connection.call_args[0][0]
        errback(Mock(), 2)
        assert error.call_args[0][3] == 'Trying again in 2.00 seconds... (1/3)'
        errback(Mock(), 4)
        assert error.call_args[0][3] == 'Trying again in 4.00 seconds... (2/3)'
        errback(Mock(), 6)
        assert error.call_args[0][3] == 'Trying again in 6.00 seconds... (3/3)'

    def test_cancel_long_running_tasks_on_connection_loss(self):
        c = self.get_consumer()
        c.app.conf.worker_cancel_long_running_tasks_on_connection_loss = True

        mock_request_acks_late_not_acknowledged = Mock()
        mock_request_acks_late_not_acknowledged.task.acks_late = True
        mock_request_acks_late_not_acknowledged.acknowledged = False
        mock_request_acks_late_acknowledged = Mock()
        mock_request_acks_late_acknowledged.task.acks_late = True
        mock_request_acks_late_acknowledged.acknowledged = True
        mock_request_acks_early = Mock()
        mock_request_acks_early.task.acks_late = False
        mock_request_acks_early.acknowledged = False

        active_requests.add(mock_request_acks_late_not_acknowledged)
        active_requests.add(mock_request_acks_late_acknowledged)
        active_requests.add(mock_request_acks_early)

        c.on_connection_error_after_connected(Mock())

        mock_request_acks_late_not_acknowledged.cancel.assert_called_once_with(c.pool)
        mock_request_acks_late_acknowledged.cancel.assert_not_called()
        mock_request_acks_early.cancel.assert_not_called()

        active_requests.clear()

    def test_cancel_long_running_tasks_on_connection_loss__warning(self):
        c = self.get_consumer()
        c.app.conf.worker_cancel_long_running_tasks_on_connection_loss = False

        with pytest.deprecated_call(match=CANCEL_TASKS_BY_DEFAULT):
            c.on_connection_error_after_connected(Mock())

    @pytest.mark.usefixtures('depends_on_current_app')
    def test_cancel_all_unacked_requests(self):
        c = self.get_consumer()

        mock_request_acks_late_not_acknowledged = Mock(id='1')
        mock_request_acks_late_not_acknowledged.task.acks_late = True
        mock_request_acks_late_not_acknowledged.acknowledged = False
        mock_request_acks_late_acknowledged = Mock(id='2')
        mock_request_acks_late_acknowledged.task.acks_late = True
        mock_request_acks_late_acknowledged.acknowledged = True
        mock_request_acks_early = Mock(id='3')
        mock_request_acks_early.task.acks_late = False

        active_requests.add(mock_request_acks_late_not_acknowledged)
        active_requests.add(mock_request_acks_late_acknowledged)
        active_requests.add(mock_request_acks_early)

        c.cancel_all_unacked_requests()

        mock_request_acks_late_not_acknowledged.cancel.assert_called_once_with(c.pool)
        mock_request_acks_late_acknowledged.cancel.assert_not_called()
        mock_request_acks_early.cancel.assert_called_once_with(c.pool)

        active_requests.clear()

    @pytest.mark.parametrize("broker_connection_retry", [True, False])
    @pytest.mark.parametrize("broker_connection_retry_on_startup", [None, False])
    @pytest.mark.parametrize("first_connection_attempt", [True, False])
    def test_ensure_connected(self, subtests, broker_connection_retry, broker_connection_retry_on_startup,
                              first_connection_attempt):
        c = self.get_consumer()
        c.first_connection_attempt = first_connection_attempt
        c.app.conf.broker_connection_retry_on_startup = broker_connection_retry_on_startup
        c.app.conf.broker_connection_retry = broker_connection_retry

        if broker_connection_retry is False:
            if broker_connection_retry_on_startup is None:
                with subtests.test("Deprecation warning when startup is None"):
                    with pytest.deprecated_call():
                        c.ensure_connected(Mock())

            with subtests.test("Does not retry when connect throws an error and retry is set to false"):
                conn = Mock()
                conn.connect.side_effect = ConnectionError()
                with pytest.raises(ConnectionError):
                    c.ensure_connected(conn)

    def test_disable_prefetch_not_enabled(self):
        """Test that disable_prefetch doesn't affect behavior when disabled"""
        self.app.conf.worker_disable_prefetch = False
        
        # Test the core logic by creating a mock consumer and Tasks instance
        from celery.worker.consumer.tasks import Tasks
        consumer = Mock()
        consumer.app = self.app
        consumer.pool = Mock()
        consumer.pool.num_processes = 4
        consumer.controller = Mock()
        consumer.controller.max_concurrency = None
        consumer.initial_prefetch_count = 16
        consumer.connection = Mock()
        consumer.connection.default_channel = Mock()
        consumer.update_strategies = Mock()
        consumer.on_decode_error = Mock()
        
        # Mock task consumer
        consumer.task_consumer = Mock()
        consumer.task_consumer.channel = Mock()
        consumer.task_consumer.channel.qos = Mock()
        original_can_consume = Mock(return_value=True)
        consumer.task_consumer.channel.qos.can_consume = original_can_consume
        consumer.task_consumer.qos = Mock()
        
        consumer.app.amqp = Mock()
        consumer.app.amqp.TaskConsumer = Mock(return_value=consumer.task_consumer)
        
        tasks_instance = Tasks(consumer)
        tasks_instance.start(consumer)
        
        # Should not modify can_consume method when disabled
        assert consumer.task_consumer.channel.qos.can_consume == original_can_consume

    def test_disable_prefetch_enabled_basic(self):
        """Test that disable_prefetch modifies can_consume when enabled"""
        self.app.conf.worker_disable_prefetch = True
        
        # Test the core logic by creating a mock consumer and Tasks instance
        from celery.worker.consumer.tasks import Tasks
        consumer = Mock()
        consumer.app = self.app
        consumer.pool = Mock()
        consumer.pool.num_processes = 4
        consumer.controller = Mock()
        consumer.controller.max_concurrency = None
        consumer.initial_prefetch_count = 16
        consumer.connection = Mock()
        consumer.connection.default_channel = Mock()
        consumer.update_strategies = Mock()
        consumer.on_decode_error = Mock()
        
        # Mock task consumer
        consumer.task_consumer = Mock()
        consumer.task_consumer.channel = Mock()
        consumer.task_consumer.channel.qos = Mock()
        original_can_consume = Mock(return_value=True)
        consumer.task_consumer.channel.qos.can_consume = original_can_consume
        consumer.task_consumer.qos = Mock()
        
        consumer.app.amqp = Mock()
        consumer.app.amqp.TaskConsumer = Mock(return_value=consumer.task_consumer)
        
        tasks_instance = Tasks(consumer)
        
        with patch('celery.worker.state.reserved_requests', []):
            tasks_instance.start(consumer)
            
            # Should modify can_consume method when enabled
            assert callable(consumer.task_consumer.channel.qos.can_consume)
            assert consumer.task_consumer.channel.qos.can_consume != original_can_consume

    def test_disable_prefetch_respects_reserved_requests_limit(self):
        """Test that disable_prefetch respects reserved requests limit"""
        self.app.conf.worker_disable_prefetch = True
        
        # Test the core logic by creating a mock consumer and Tasks instance
        from celery.worker.consumer.tasks import Tasks
        consumer = Mock()
        consumer.app = self.app
        consumer.pool = Mock()
        consumer.pool.num_processes = 4
        consumer.controller = Mock()
        consumer.controller.max_concurrency = None
        consumer.initial_prefetch_count = 16
        consumer.connection = Mock()
        consumer.connection.default_channel = Mock()
        consumer.update_strategies = Mock()
        consumer.on_decode_error = Mock()
        
        # Mock task consumer
        consumer.task_consumer = Mock()
        consumer.task_consumer.channel = Mock()
        consumer.task_consumer.channel.qos = Mock()
        consumer.task_consumer.channel.qos.can_consume = Mock(return_value=True)
        consumer.task_consumer.qos = Mock()
        
        consumer.app.amqp = Mock()
        consumer.app.amqp.TaskConsumer = Mock(return_value=consumer.task_consumer)
        
        tasks_instance = Tasks(consumer)
        
        # Mock 4 reserved requests (at limit of 4)
        mock_requests = [Mock(), Mock(), Mock(), Mock()]
        with patch('celery.worker.state.reserved_requests', mock_requests):
            tasks_instance.start(consumer)
            
            # Should not be able to consume when at limit
            assert consumer.task_consumer.channel.qos.can_consume() is False

    def test_disable_prefetch_respects_autoscale_max_concurrency(self):
        """Test that disable_prefetch respects autoscale max_concurrency limit"""
        self.app.conf.worker_disable_prefetch = True
        
        # Test the core logic by creating a mock consumer and Tasks instance
        from celery.worker.consumer.tasks import Tasks
        consumer = Mock()
        consumer.app = self.app
        consumer.pool = Mock()
        consumer.pool.num_processes = 4
        consumer.controller = Mock()
        consumer.controller.max_concurrency = 2  # Lower than pool processes
        consumer.initial_prefetch_count = 16
        consumer.connection = Mock()
        consumer.connection.default_channel = Mock()
        consumer.update_strategies = Mock()
        consumer.on_decode_error = Mock()
        
        # Mock task consumer
        consumer.task_consumer = Mock()
        consumer.task_consumer.channel = Mock()
        consumer.task_consumer.channel.qos = Mock()
        consumer.task_consumer.channel.qos.can_consume = Mock(return_value=True)
        consumer.task_consumer.qos = Mock()
        
        consumer.app.amqp = Mock()
        consumer.app.amqp.TaskConsumer = Mock(return_value=consumer.task_consumer)
        
        tasks_instance = Tasks(consumer)
        
        # Mock 2 reserved requests (at autoscale limit of 2)
        mock_requests = [Mock(), Mock()]
        with patch('celery.worker.state.reserved_requests', mock_requests):
            tasks_instance.start(consumer)
            
            # Should not be able to consume when at autoscale limit
            assert consumer.task_consumer.channel.qos.can_consume() is False


@pytest.mark.parametrize(
    "broker_connection_retry_on_startup,is_connection_loss_on_startup",
    [
        pytest.param(False, True, id='shutdown on connection loss on startup'),
        pytest.param(None, True, id='shutdown on connection loss on startup when retry on startup is undefined'),
        pytest.param(False, False, id='shutdown on connection loss not on startup but startup is defined as false'),
        pytest.param(None, False, id='shutdown on connection loss not on startup and startup is not defined'),
        pytest.param(True, False, id='shutdown on connection loss not on startup but startup is defined as true'),
    ]
)
class test_Consumer_WorkerShutdown(ConsumerTestCase):

    def test_start_raises_connection_error(self,
                                           broker_connection_retry_on_startup,
                                           is_connection_loss_on_startup,
                                           caplog, subtests):
        c = self.get_consumer()
        c.first_connection_attempt = True if is_connection_loss_on_startup else False
        c.app.conf['broker_connection_retry'] = False
        c.app.conf['broker_connection_retry_on_startup'] = broker_connection_retry_on_startup
        c.blueprint.start.side_effect = ConnectionError()

        with subtests.test("Consumer raises WorkerShutdown on connection restart"):
            with pytest.raises(WorkerShutdown):
                c.start()

        record = caplog.records[0]
        with subtests.test("Critical error log message is outputted to the screen"):
            assert record.levelname == "CRITICAL"
            action = "establish" if is_connection_loss_on_startup else "re-establish"
            expected_prefix = f"Retrying to {action}"
            assert record.msg.startswith(expected_prefix)
            conn_type_name = c._get_connection_retry_type(
                is_connection_loss_on_startup
            )
            expected_connection_retry_type = f"app.conf.{conn_type_name}=False"
            assert expected_connection_retry_type in record.msg


class test_Consumer_PerformPendingOperations(ConsumerTestCase):

    def test_perform_pending_operations_all_success(self):
        """
        Test that all pending operations are processed successfully when `once=False`.
        """
        c = self.get_consumer(no_hub=True)

        # Create mock operations
        mock_operation_1 = Mock()
        mock_operation_2 = Mock()

        # Add mock operations to _pending_operations
        c._pending_operations = [mock_operation_1, mock_operation_2]

        # Call perform_pending_operations
        c.perform_pending_operations()

        # Assert that all operations were called
        mock_operation_1.assert_called_once()
        mock_operation_2.assert_called_once()

        # Ensure all pending operations are cleared
        assert len(c._pending_operations) == 0

    def test_perform_pending_operations_with_exception(self):
        """
        Test that pending operations are processed even if one raises an exception, and
        the exception is logged when `once=False`.
        """
        c = self.get_consumer(no_hub=True)

        # Mock operations: one failing, one successful
        mock_operation_fail = Mock(side_effect=Exception("Test Exception"))
        mock_operation_success = Mock()

        # Add operations to _pending_operations
        c._pending_operations = [mock_operation_fail, mock_operation_success]

        # Patch logger to avoid logging during the test
        with patch('celery.worker.consumer.consumer.logger.exception') as mock_logger:
            # Call perform_pending_operations
            c.perform_pending_operations()

            # Assert that both operations were attempted
            mock_operation_fail.assert_called_once()
            mock_operation_success.assert_called_once()

            # Ensure the exception was logged
            mock_logger.assert_called_once()

            # Ensure all pending operations are cleared
            assert len(c._pending_operations) == 0


class test_Heart:

    def test_start(self):
        c = Mock()
        c.timer = Mock()
        c.event_dispatcher = Mock()

        with patch('celery.worker.heartbeat.Heart') as hcls:
            h = Heart(c)
            assert h.enabled
            assert h.heartbeat_interval is None
            assert c.heart is None

            h.start(c)
            assert c.heart
            hcls.assert_called_with(c.timer, c.event_dispatcher,
                                    h.heartbeat_interval)
            c.heart.start.assert_called_with()

    def test_start_heartbeat_interval(self):
        c = Mock()
        c.timer = Mock()
        c.event_dispatcher = Mock()

        with patch('celery.worker.heartbeat.Heart') as hcls:
            h = Heart(c, False, 20)
            assert h.enabled
            assert h.heartbeat_interval == 20
            assert c.heart is None

            h.start(c)
            assert c.heart
            hcls.assert_called_with(c.timer, c.event_dispatcher,
                                    h.heartbeat_interval)
            c.heart.start.assert_called_with()


class test_Tasks:

    def setup_method(self):
        self.c = Mock()
        self.c.app.conf.worker_detect_quorum_queues = True
        self.c.connection.qos_semantics_matches_spec = False

    def test_stop(self):
        c = self.c
        tasks = Tasks(c)
        assert c.task_consumer is None
        assert c.qos is None

        c.task_consumer = Mock()
        tasks.stop(c)

    def test_stop_already_stopped(self):
        c = self.c
        tasks = Tasks(c)
        tasks.stop(c)

    def test_detect_quorum_queues_positive(self):
        c = self.c
        self.c.connection.transport.driver_type = 'amqp'
        c.app.amqp.queues = {"celery": Mock(queue_arguments={"x-queue-type": "quorum"})}
        result, name = detect_quorum_queues(c.app, c.connection.transport.driver_type)
        assert result
        assert name == "celery"

    def test_detect_quorum_queues_negative(self):
        c = self.c
        self.c.connection.transport.driver_type = 'amqp'
        c.app.amqp.queues = {"celery": Mock(queue_arguments=None)}
        result, name = detect_quorum_queues(c.app, c.connection.transport.driver_type)
        assert not result
        assert name == ""

    def test_detect_quorum_queues_not_rabbitmq(self):
        c = self.c
        self.c.connection.transport.driver_type = 'redis'
        result, name = detect_quorum_queues(c.app, c.connection.transport.driver_type)
        assert not result
        assert name == ""

    def test_qos_global_worker_detect_quorum_queues_false(self):
        c = self.c
        c.app.conf.worker_detect_quorum_queues = False
        tasks = Tasks(c)
        assert tasks.qos_global(c) is True

    def test_qos_global_worker_detect_quorum_queues_true_no_quorum_queues(self):
        c = self.c
        c.app.amqp.queues = {"celery": Mock(queue_arguments=None)}
        tasks = Tasks(c)
        assert tasks.qos_global(c) is True

    def test_qos_global_worker_detect_quorum_queues_true_with_quorum_queues(self):
        c = self.c
        self.c.connection.transport.driver_type = 'amqp'
        c.app.amqp.queues = {"celery": Mock(queue_arguments={"x-queue-type": "quorum"})}
        tasks = Tasks(c)
        assert tasks.qos_global(c) is False

    def test_log_when_qos_is_false(self, caplog):
        c = self.c
        c.connection.transport.driver_type = 'amqp'
        c.app.conf.broker_native_delayed_delivery = True
        c.app.amqp.queues = {"celery": Mock(queue_arguments={"x-queue-type": "quorum"})}
        tasks = Tasks(c)

        with caplog.at_level(logging.INFO):
            tasks.start(c)

        assert len(caplog.records) == 1

        record = caplog.records[0]
        assert record.levelname == "INFO"
        assert record.msg == "Global QoS is disabled. Prefetch count in now static."


class test_Agent:

    def test_start(self):
        c = Mock()
        agent = Agent(c)
        agent.instantiate = Mock()
        agent.agent_cls = 'foo:Agent'
        assert agent.create(c) is not None
        agent.instantiate.assert_called_with(agent.agent_cls, c.connection)


class test_Mingle:

    def test_start_no_replies(self):
        c = Mock()
        c.app.connection_for_read = _amqp_connection()
        mingle = Mingle(c)
        I = c.app.control.inspect.return_value = Mock()
        I.hello.return_value = {}
        mingle.start(c)

    def test_start(self):
        c = Mock()
        c.app.connection_for_read = _amqp_connection()
        mingle = Mingle(c)
        assert mingle.enabled

        Aig = LimitedSet()
        Big = LimitedSet()
        Aig.add('Aig-1')
        Aig.add('Aig-2')
        Big.add('Big-1')

        I = c.app.control.inspect.return_value = Mock()
        I.hello.return_value = {
            'A@example.com': {
                'clock': 312,
                'revoked': Aig._data,
            },
            'B@example.com': {
                'clock': 29,
                'revoked': Big._data,
            },
            'C@example.com': {
                'error': 'unknown method',
            },
        }

        our_revoked = c.controller.state.revoked = LimitedSet()

        mingle.start(c)
        I.hello.assert_called_with(c.hostname, our_revoked._data)
        c.app.clock.adjust.assert_has_calls([
            call(312), call(29),
        ], any_order=True)
        assert 'Aig-1' in our_revoked
        assert 'Aig-2' in our_revoked
        assert 'Big-1' in our_revoked


def _amqp_connection():
    connection = ContextMock(name='Connection')
    connection.return_value = ContextMock(name='connection')
    connection.return_value.transport.driver_type = 'amqp'
    return connection


class test_Gossip:

    def test_init(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        assert g.enabled
        assert c.gossip is g

    def test_callbacks(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        on_node_join = Mock(name='on_node_join')
        on_node_join2 = Mock(name='on_node_join2')
        on_node_leave = Mock(name='on_node_leave')
        on_node_lost = Mock(name='on.node_lost')
        g.on.node_join.add(on_node_join)
        g.on.node_join.add(on_node_join2)
        g.on.node_leave.add(on_node_leave)
        g.on.node_lost.add(on_node_lost)

        worker = Mock(name='worker')
        g.on_node_join(worker)
        on_node_join.assert_called_with(worker)
        on_node_join2.assert_called_with(worker)
        g.on_node_leave(worker)
        on_node_leave.assert_called_with(worker)
        g.on_node_lost(worker)
        on_node_lost.assert_called_with(worker)

    def test_election(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.start(c)
        g.election('id', 'topic', 'action')
        assert g.consensus_replies['id'] == []
        g.dispatcher.send.assert_called_with(
            'worker-elect', id='id', topic='topic', cver=1, action='action',
        )

    def test_call_task(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.start(c)
        signature = g.app.signature = Mock(name='app.signature')
        task = Mock()
        g.call_task(task)
        signature.assert_called_with(task)
        signature.return_value.apply_async.assert_called_with()

        signature.return_value.apply_async.side_effect = MemoryError()
        with patch('celery.worker.consumer.gossip.logger') as logger:
            g.call_task(task)
            logger.exception.assert_called()

    def Event(self, id='id', clock=312,
              hostname='foo@example.com', pid=4312,
              topic='topic', action='action', cver=1):
        return {
            'id': id,
            'clock': clock,
            'hostname': hostname,
            'pid': pid,
            'topic': topic,
            'action': action,
            'cver': cver,
        }

    def test_on_elect(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.start(c)

        event = self.Event('id1')
        g.on_elect(event)
        in_heap = g.consensus_requests['id1']
        assert in_heap
        g.dispatcher.send.assert_called_with('worker-elect-ack', id='id1')

        event.pop('clock')
        with patch('celery.worker.consumer.gossip.logger') as logger:
            g.on_elect(event)
            logger.exception.assert_called()

    def Consumer(self, hostname='foo@x.com', pid=4312):
        c = Mock()
        c.app.connection = _amqp_connection()
        c.hostname = hostname
        c.pid = pid
        c.app.events.Receiver.return_value = Mock(accept=[])
        return c

    def setup_election(self, g, c):
        g.start(c)
        g.clock = self.app.clock
        assert 'idx' not in g.consensus_replies
        assert g.on_elect_ack({'id': 'idx'}) is None

        g.state.alive_workers.return_value = [
            'foo@x.com', 'bar@x.com', 'baz@x.com',
        ]
        g.consensus_replies['id1'] = []
        g.consensus_requests['id1'] = []
        e1 = self.Event('id1', 1, 'foo@x.com')
        e2 = self.Event('id1', 2, 'bar@x.com')
        e3 = self.Event('id1', 3, 'baz@x.com')
        g.on_elect(e1)
        g.on_elect(e2)
        g.on_elect(e3)
        assert len(g.consensus_requests['id1']) == 3

        with patch('celery.worker.consumer.gossip.info'):
            g.on_elect_ack(e1)
            assert len(g.consensus_replies['id1']) == 1
            g.on_elect_ack(e2)
            assert len(g.consensus_replies['id1']) == 2
            g.on_elect_ack(e3)
            with pytest.raises(KeyError):
                g.consensus_replies['id1']

    def test_on_elect_ack_win(self):
        c = self.Consumer(hostname='foo@x.com')  # I will win
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        handler = g.election_handlers['topic'] = Mock()
        self.setup_election(g, c)
        handler.assert_called_with('action')

    def test_on_elect_ack_lose(self):
        c = self.Consumer(hostname='bar@x.com')  # I will lose
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        handler = g.election_handlers['topic'] = Mock()
        self.setup_election(g, c)
        handler.assert_not_called()

    def test_on_elect_ack_win_but_no_action(self):
        c = self.Consumer(hostname='foo@x.com')  # I will win
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.election_handlers = {}
        with patch('celery.worker.consumer.gossip.logger') as logger:
            self.setup_election(g, c)
            logger.exception.assert_called()

    def test_on_node_join(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        with patch('celery.worker.consumer.gossip.debug') as debug:
            g.on_node_join(c)
            debug.assert_called_with('%s joined the party', 'foo@x.com')

    def test_on_node_leave(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        with patch('celery.worker.consumer.gossip.debug') as debug:
            g.on_node_leave(c)
            debug.assert_called_with('%s left', 'foo@x.com')

    def test_on_node_lost(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        with patch('celery.worker.consumer.gossip.info') as info:
            g.on_node_lost(c)
            info.assert_called_with('missed heartbeat from %s', 'foo@x.com')

    def test_register_timer(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.register_timer()
        c.timer.call_repeatedly.assert_called_with(g.interval, g.periodic)
        tref = g._tref
        g.register_timer()
        tref.cancel.assert_called_with()

    def test_periodic(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        g.on_node_lost = Mock()
        state = g.state = Mock()
        worker = Mock()
        state.workers = {'foo': worker}
        worker.alive = True
        worker.hostname = 'foo'
        g.periodic()

        worker.alive = False
        g.periodic()
        g.on_node_lost.assert_called_with(worker)
        with pytest.raises(KeyError):
            state.workers['foo']

    def test_on_message__task(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        assert g.enabled
        message = Mock(name='message')
        message.delivery_info = {'routing_key': 'task.failed'}
        g.on_message(Mock(name='prepare'), message)

    def test_on_message(self):
        c = self.Consumer()
        c.app.connection_for_read = _amqp_connection()
        g = Gossip(c)
        assert g.enabled
        prepare = Mock()
        prepare.return_value = 'worker-online', {}
        c.app.events.State.assert_called_with(
            on_node_join=g.on_node_join,
            on_node_leave=g.on_node_leave,
            max_tasks_in_memory=1,
        )
        g.update_state = Mock()
        worker = Mock()
        g.on_node_join = Mock()
        g.on_node_leave = Mock()
        g.update_state.return_value = worker, 1
        message = Mock()
        message.delivery_info = {'routing_key': 'worker-online'}
        message.headers = {'hostname': 'other'}

        handler = g.event_handlers['worker-online'] = Mock()
        g.on_message(prepare, message)
        handler.assert_called_with(message.payload)
        g.event_handlers = {}

        g.on_message(prepare, message)

        message.delivery_info = {'routing_key': 'worker-offline'}
        prepare.return_value = 'worker-offline', {}
        g.on_message(prepare, message)

        message.delivery_info = {'routing_key': 'worker-baz'}
        prepare.return_value = 'worker-baz', {}
        g.update_state.return_value = worker, 0
        g.on_message(prepare, message)

        message.headers = {'hostname': g.hostname}
        g.on_message(prepare, message)
        g.clock.forward.assert_called_with()
