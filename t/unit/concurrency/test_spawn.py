import os
from unittest.mock import patch

import pytest

from celery.concurrency import spawn


class test_spawn_TaskPool:
    @patch('billiard.set_start_method')
    def test_on_start_sets_spawn(self, set_method):
        pool = spawn.TaskPool(1)
        with patch.dict(os.environ, {}, clear=True):
            pool.on_start()
            set_method.assert_called_with('spawn', force=True)
            assert os.environ['FORKED_BY_MULTIPROCESSING'] == '1'

