"""Worker Task Consumer Bootstep."""

from __future__ import annotations

from kombu.common import QoS, ignore_errors

from celery import bootsteps
from celery.utils.log import get_logger
from celery.utils.quorum_queues import detect_quorum_queues

from .mingle import Mingle

__all__ = ('Tasks',)


logger = get_logger(__name__)
debug = logger.debug


class Tasks(bootsteps.StartStopStep):
    """Bootstep starting the task message consumer."""

    requires = (Mingle,)

    def __init__(self, c, **kwargs):
        c.task_consumer = c.qos = None
        super().__init__(c, **kwargs)

    def start(self, c):
        """Start task consumer."""
        c.update_strategies()

        qos_global = self.qos_global(c)

        # set initial prefetch count
        c.connection.default_channel.basic_qos(
            0, c.initial_prefetch_count, qos_global,
        )

        c.task_consumer = c.app.amqp.TaskConsumer(
            c.connection, on_decode_error=c.on_decode_error,
        )

        def set_prefetch_count(prefetch_count):
            return c.task_consumer.qos(
                prefetch_count=prefetch_count,
                apply_global=qos_global,
            )
        c.qos = QoS(set_prefetch_count, c.initial_prefetch_count)

        if c.app.conf.worker_disable_prefetch:
            from types import MethodType

            from celery.worker import state
            channel_qos = c.task_consumer.channel.qos
            original_can_consume = channel_qos.can_consume

            def can_consume(self):
                limit = getattr(c.controller, "max_concurrency", None)
                if limit is None:
                    limit = c.pool.num_processes
                if len(state.reserved_requests) >= limit:
                    return False
                return original_can_consume()

            channel_qos.can_consume = MethodType(can_consume, channel_qos)

    def stop(self, c):
        """Stop task consumer."""
        if c.task_consumer:
            debug('Canceling task consumer...')
            ignore_errors(c, c.task_consumer.cancel)

    def shutdown(self, c):
        """Shutdown task consumer."""
        if c.task_consumer:
            self.stop(c)
            debug('Closing consumer channel...')
            ignore_errors(c, c.task_consumer.close)
            c.task_consumer = None

    def info(self, c):
        """Return task consumer info."""
        return {'prefetch_count': c.qos.value if c.qos else 'N/A'}

    def qos_global(self, c) -> bool:
        """Determine if global QoS should be applied.

        Additional information:
            https://www.rabbitmq.com/docs/consumer-prefetch
            https://www.rabbitmq.com/docs/quorum-queues#global-qos
        """
        # - RabbitMQ 3.3 completely redefines how basic_qos works...
        # This will detect if the new qos semantics is in effect,
        # and if so make sure the 'apply_global' flag is set on qos updates.
        qos_global = not c.connection.qos_semantics_matches_spec

        if c.app.conf.worker_detect_quorum_queues:
            using_quorum_queues, qname = detect_quorum_queues(c.app, c.connection.transport.driver_type)

            if using_quorum_queues:
                qos_global = False
                logger.info("Global QoS is disabled. Prefetch count in now static.")

        return qos_global
