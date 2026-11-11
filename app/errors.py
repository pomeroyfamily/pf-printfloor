"""Errors raised by printer drivers and transports. The lifecycle loop maps them to card states."""
from __future__ import annotations

from .models import ConnState


class DriverError(Exception):
    conn_state = ConnState.ERROR


class PrinterUnreachable(DriverError):
    conn_state = ConnState.OFFLINE


class AuthFailed(DriverError):
    conn_state = ConnState.AUTH_ERROR


class PrinterBusy(DriverError):
    """Printer refused the connection because its client slots are full."""
    conn_state = ConnState.BUSY
