"""Bounded local journal for redacted Postiz HTTP diagnostics."""

import json
import logging
import os
import threading
import time

from .browser_runtime import profile_lock
from .config import AgentError
from .monitor_config import private_path

MAX_BYTES = 1024 * 1024
_NAME = 'data/postiz-http.jsonl'
_LOCK = 'data/postiz-http.lock'
_THREAD_LOCK = threading.Lock()


class PostizHttpJournal:
    def __init__(self, root):
        self.root = root
        self.warned = False

    def append(self, event):
        try:
            line = (json.dumps(event, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
            if len(line) > MAX_BYTES:
                raise ValueError('POSTIZ_HTTP_JOURNAL_EVENT_TOO_LARGE')
            with _THREAD_LOCK:
                for attempt in range(3):
                    try:
                        with profile_lock(private_path(self.root, _LOCK)):
                            self._write(line)
                        return
                    except AgentError as exc:
                        if exc.code != 'BROWSER_PROFILE_BUSY' or attempt == 2:
                            raise
                        time.sleep(0.01)
        except (AgentError, OSError, ValueError):
            # A local diagnostic failure must never change an external outcome.
            if not self.warned:
                self.warned = True
                logging.getLogger(__name__).warning('POSTIZ_HTTP_JOURNAL_UNAVAILABLE')

    def _write(self, line):
        current = private_path(self.root, _NAME)
        if current.exists() and current.stat().st_size + len(line) > MAX_BYTES:
            first = private_path(self.root, _NAME + '.1')
            second = private_path(self.root, _NAME + '.2')
            if first.exists():
                os.replace(first, second)
            os.replace(current, first)
        fd = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(line)
