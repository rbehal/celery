"""Spawn execution pool."""
from .prefork import TaskPool as PreforkTaskPool

__all__ = ("TaskPool",)


class TaskPool(PreforkTaskPool):
    """Multiprocessing Pool using the 'spawn' start method."""

    def __init__(self, *args, **kwargs):
        kwargs['forking_enable'] = False
        super().__init__(*args, **kwargs)
