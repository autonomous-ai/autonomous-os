"""GELF HTTP log handler for centralized logging to Graylog."""

import logging
import os
import socket
import threading

_LEVEL_MAP = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


class GELFHandler(logging.Handler):
    """Sends log records to a GELF HTTP endpoint. Fire-and-forget."""

    def __init__(self, service_name: str = "hal"):
        super().__init__(level=logging.INFO)
        self._host = socket.gethostname() or "hal"
        self._pid = os.getpid()
        self._service_name = service_name
        self._session = None
        self._url = os.getenv("GELF_URL", "")
        self._auth = (os.getenv("GELF_USERNAME", ""), os.getenv("GELF_PASSWORD", ""))

    def _get_session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.auth = self._auth
            self._session.headers["Content-Type"] = "application/json"
        return self._session

    def emit(self, record):
        if not self._url:
            return
        try:
            msg = {
                "version": "1.1",
                "host": self._host,
                "short_message": self.format(record),
                "timestamp": record.created,
                "level": _LEVEL_MAP.get(record.levelno, 6),
                "_service_name": self._service_name,
                "_level_name": record.levelname,
                "_logger": record.name,
                "_pid": self._pid,
            }
            threading.Thread(target=self._send, args=(msg,), daemon=True).start()
        except Exception:
            pass

    def _send(self, msg):
        try:
            self._get_session().post(self._url, json=msg, timeout=3)
        except Exception:
            pass

    def set_host(self, host: str):
        if host:
            self._host = host
