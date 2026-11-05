"""Ordered, bounded settings persistence; no GUI objects cross this boundary."""
import json
import os
import tempfile
import threading
from pathlib import Path


class SettingsWriter:
    def __init__(self, path, logger):
        self.path = Path(path)
        self.logger = logger
        self._condition = threading.Condition()
        self._pending = None
        self._stopping = False
        self._last_payload = None
        self._error = None
        self._thread = threading.Thread(target=self._run, name='Magellan-settings', daemon=True)
        self._thread.start()

    def submit(self, snapshot):
        """Take ownership of an independent snapshot, replacing any queued one."""
        with self._condition:
            if self._stopping:
                raise RuntimeError('Settings writer has already stopped')
            self._pending = snapshot
            self._condition.notify()

    def finish(self, timeout=5.0):
        """Drain the newest state before backups; bound shutdown if I/O hangs."""
        with self._condition:
            self._stopping = True
            self._condition.notify()
        self._thread.join(timeout)
        return not self._thread.is_alive() and self._error is None

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix=self.path.stem+'-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning('Could not remove temporary settings file %s', temporary, exc_info=True)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._stopping)
                if self._pending is None:
                    return
                snapshot, self._pending = self._pending, None
            try:
                payload = json.dumps(snapshot, sort_keys=True)
                if payload != self._last_payload:
                    self._write(payload)
                    self._last_payload = payload
                self._error = None
            except Exception as error:
                self._error = error
                self.logger.exception('Could not save application settings')
