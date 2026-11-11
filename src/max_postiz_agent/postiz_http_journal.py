"""Bounded local journal for redacted Postiz HTTP diagnostics."""

import hashlib
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

    def events_for_post(self, post_id):
        digest = hashlib.sha256(post_id.encode()).hexdigest()
        try:
            events = []
            with _THREAD_LOCK, profile_lock(private_path(self.root, _LOCK)):
                for suffix in ('.2', '.1', ''):
                    path = private_path(self.root, _NAME + suffix)
                    if not path.exists():
                        continue
                    if not path.is_file():
                        raise ValueError('Invalid journal file')
                    with path.open('rb') as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES or (raw and not raw.endswith(b'\n')):
                        raise ValueError('Invalid journal size or incomplete record')
                    for line in raw.splitlines():
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError('Invalid journal record')
                        if event.get('post_id_sha256') == digest:
                            events.append(event)
            return events
        except (OSError, ValueError, AgentError) as exc:
            raise AgentError('PREAPPROVAL_POSTIZ_JOURNAL_INVALID', 409) from exc

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
