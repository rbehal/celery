"""Spawn execution pool (multiprocessing using the spawn start method)."""
import os

from billiard import set_start_method

from celery.concurrency import prefork

__all__ = ("TaskPool",)


class TaskPool(prefork.TaskPool):
    """TaskPool that uses the spawn start method."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("forking_enable", False)
        super().__init__(*args, **kwargs)

    def on_start(self):
        try:
            set_start_method("spawn", force=True)
        except Exception:
            pass
        os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")
        super().on_start()
