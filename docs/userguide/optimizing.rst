.. _guide-optimizing:

============
 Optimizing
============

Introduction
============
The default configuration makes a lot of compromises. It's not optimal for
any single case, but works well enough for most situations.

There are optimizations that can be applied based on specific use cases.

Optimizations can apply to different properties of the running environment,
be it the time tasks take to execute, the amount of memory used, or
responsiveness at times of high load.

Ensuring Operations
===================

In the book Programming Pearls, Jon Bentley presents the concept of
back-of-the-envelope calculations by asking the question;

    ❝ How much water flows out of the Mississippi River in a day? ❞

The point of this exercise [*]_ is to show that there's a limit
to how much data a system can process in a timely manner.
Back of the envelope calculations can be used as a means to plan for this
ahead of time.

In Celery; If a task takes 10 minutes to complete,
and there are 10 new tasks coming in every minute, the queue will never
be empty. This is why it's very important
that you monitor queue lengths!

A way to do this is by :ref:`using Munin <monitoring-munin>`.
You should set up alerts, that'll notify you as soon as any queue has
reached an unacceptable size. This way you can take appropriate action
like adding new worker nodes, or revoking unnecessary tasks.

.. _`The back of the envelope`:
    http://books.google.com/books?id=kse_7qbWbjsC&pg=PA67

.. _optimizing-general-settings:

General Settings
================

.. _optimizing-connection-pools:

Broker Connection Pools
-----------------------

The broker connection pool is enabled by default since version 2.5.

You can tweak the :setting:`broker_pool_limit` setting to minimize
contention, and the value should be based on the number of
active threads/green-threads using broker connections.

.. _optimizing-transient-queues:

Using Transient Queues
----------------------

Queues created by Celery are persistent by default. This means that
the broker will write messages to disk to ensure that the tasks will
be executed even if the broker is restarted.

But in some cases it's fine that the message is lost, so not all tasks
require durability. You can create a *transient* queue for these tasks
to improve performance:

.. code-block:: python

    from kombu import Exchange, Queue

    task_queues = (
        Queue('celery', routing_key='celery'),
        Queue('transient', Exchange('transient', delivery_mode=1),
              routing_key='transient', durable=False),
    )


or by using :setting:`task_routes`:

.. code-block:: python

    task_routes = {
        'proj.tasks.add': {'queue': 'celery', 'delivery_mode': 'transient'}
    }


The ``delivery_mode`` changes how the messages to this queue are delivered.
A value of one means that the message won't be written to disk, and a value
of two (default) means that the message can be written to disk.

To direct a task to your new transient queue you can specify the queue
argument (or use the :setting:`task_routes` setting):

.. code-block:: python

    task.apply_async(args, queue='transient')

For more information see the :ref:`routing guide <guide-routing>`.

.. _optimizing-worker-settings:

Worker Settings
===============

.. _optimizing-prefetch-limit:

Prefetch Limits
---------------

*Prefetch* is a term inherited from AMQP that's often misunderstood
by users.

The prefetch limit is a **limit** for the number of tasks (messages) a worker
can reserve for itself. If it is zero, the worker will keep
consuming messages, not respecting that there may be other
available worker nodes that may be able to process them sooner [*]_,
or that the messages may not even fit in memory.

The workers' default prefetch count is the
:setting:`worker_prefetch_multiplier` setting multiplied by the number
of concurrency slots [*]_ (processes/threads/green-threads).

If you have many tasks with a long duration you want
the multiplier value to be *one*: meaning it'll only reserve one
task per worker process at a time.

However -- If you have many short-running tasks, and throughput/round trip
latency is important to you, this number should be large. The worker is
able to process more tasks per second if the messages have already been
prefetched, and is available in memory. You may have to experiment to find
the best value that works for you. Values like 50 or 150 might make sense in
these circumstances. Say 64, or 128.

If you have a combination of long- and short-running tasks, the best option
is to use two worker nodes that are configured separately, and route
the tasks according to the run-time (see :ref:`guide-routing`).

Reserve one task at a time
--------------------------

The task message is only deleted from the queue after the task is
:term:`acknowledged`, so if the worker crashes before acknowledging the task,
it can be redelivered to another worker (or the same after recovery).

Note that an exception is considered normal operation in Celery and it will be acknowledged.
Acknowledgments are really used to safeguard against failures that can not be normally
handled by the Python exception system (i.e. power failure, memory corruption, hardware failure, fatal signal, etc.).
For normal exceptions you should use task.retry() to retry the task.

.. seealso::

    Notes at :ref:`faq-acks_late-vs-retry`.

When using the default of early acknowledgment, having a prefetch multiplier setting
of *one*, means the worker will reserve at most one extra task for every
worker process: or in other words, if the worker is started with
:option:`-c 10 <celery worker -c>`, the worker may reserve at most 20
tasks (10 acknowledged tasks executing, and 10 unacknowledged reserved
tasks) at any time.

Often users ask if disabling "prefetching of tasks" is possible, and it is
possible with a catch. You can have a worker only reserve as many tasks as
there are worker processes, with the condition that they are acknowledged
late (10 unacknowledged tasks executing for :option:`-c 10 <celery worker -c>`)

For that, you need to enable  :term:`late acknowledgment`. Using this option over the
default behavior means a task that's already started executing will be
retried in the event of a power failure or the worker instance being killed
abruptly, so this also means the task must be :term:`idempotent`

You can enable this behavior by using the following configuration options:

.. code-block:: python

    task_acks_late = True
    worker_prefetch_multiplier = 1

If your tasks cannot be acknowledged late you can disable broker
prefetching by enabling :setting:`worker_disable_prefetch`. With this
setting the worker fetches a new task only when an execution slot is
free, preventing tasks from waiting behind long running ones on busy
workers. This can also be set from the command line using
:option:`--disable-prefetch <celery worker --disable-prefetch>`.

Memory Usage
------------

If you are experiencing high memory usage on a prefork worker, first you need
to determine whether the issue is also happening on the Celery master
process. The Celery master process's memory usage should not continue to
increase drastically after start-up. If you see this happening, it may indicate
a memory leak bug which should be reported to the Celery issue tracker.

If only your child processes have high memory usage, this indicates an issue
with your task.

Keep in mind, Python process memory usage has a "high watermark" and will not
return memory to the operating system until the child process has stopped. This
means a single high memory usage task could permanently increase the memory
usage of a child process until it's restarted. Fixing this may require adding
chunking logic to your task to reduce peak memory usage.

Celery workers have two main ways to help reduce memory usage due to the "high
watermark" and/or memory leaks in child processes: the
:setting:`worker_max_tasks_per_child` and :setting:`worker_max_memory_per_child`
settings.

You must be careful not to set these settings too low, or else your workers
will spend most of their time restarting child processes instead of processing
tasks. For example, if you use a :setting:`worker_max_tasks_per_child` of 1
and your child process takes 1 second to start, then that child process would
only be able to process a maximum of 60 tasks per minute (assuming the task ran
instantly). A similar issue can occur when your tasks always exceed
:setting:`worker_max_memory_per_child`.


.. rubric:: Footnotes

.. [*] The chapter is available to read for free here:
       `The back of the envelope`_. The book is a classic text. Highly
       recommended.

.. [*] RabbitMQ and other brokers deliver messages round-robin,
       so this doesn't apply to an active system. If there's no prefetch
       limit and you restart the cluster, there will be timing delays between
       nodes starting. If there are 3 offline nodes and one active node,
       all messages will be delivered to the active node.

.. [*] This is the concurrency setting; :setting:`worker_concurrency` or the
       :option:`celery worker -c` option.
