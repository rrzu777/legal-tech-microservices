"""The real Unix datagram must hand systemd the notifying Python PID atomically."""
import os
import socket
import tempfile

from worker import sd_notify


def test_ready_and_own_mainpid_share_one_real_datagram(monkeypatch):
    # A short directory avoids macOS's AF_UNIX path-length limit.
    with tempfile.TemporaryDirectory(prefix="notify-", dir="/tmp") as directory:
        address = directory + "/socket"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(address)
            server.settimeout(0.1)
            monkeypatch.setattr(sd_notify, "_socket_path", address)
            sd_notify.notify_ready()
            assert server.recv(8192).decode() == f"READY=1\nMAINPID={os.getpid()}"
            sd_notify.notify_watchdog()
            assert server.recv(8192) == b"WATCHDOG=1"
