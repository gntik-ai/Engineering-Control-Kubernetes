from __future__ import annotations

import os
import socket


def notify(value: str) -> None:
    """Send one sd_notify(3) datagram; a no-op outside a Type=notify unit."""
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return
    target = f"\0{address[1:]}" if address.startswith("@") else address
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(value.encode("utf-8"), target)
    except OSError:
        return


def watchdog_interval_seconds(default: float = 20.0) -> float:
    """A third of the unit's WatchdogSec (WATCHDOG_USEC), so one late ping is tolerated."""
    try:
        usec = int(os.getenv("WATCHDOG_USEC", ""))
    except ValueError:
        return default
    return usec / 3_000_000 if usec > 0 else default
