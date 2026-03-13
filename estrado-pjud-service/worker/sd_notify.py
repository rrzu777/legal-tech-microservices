# worker/sd_notify.py
"""Lightweight systemd sd_notify implementation (no external dependencies)."""

import logging
import os
import socket

logger = logging.getLogger(__name__)

_socket_path: str | None = os.environ.get("NOTIFY_SOCKET")


def notify_ready():
    """Tell systemd the service is ready."""
    _send("READY=1")


def notify_watchdog():
    """Send watchdog keep-alive to systemd."""
    _send("WATCHDOG=1")


def notify_stopping():
    """Tell systemd the service is stopping."""
    _send("STOPPING=1")


def _send(msg: str):
    if not _socket_path:
        return
    try:
        path = _socket_path
        if path.startswith("@"):
            path = "\0" + path[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(msg.encode())
    except Exception:
        logger.debug("sd_notify failed for %r", msg, exc_info=True)
