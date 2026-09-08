"""Expiring, single-use capabilities for browser transports."""

import asyncio
import secrets
import time
from dataclasses import dataclass

SESSION_TTL_SECONDS = 60


@dataclass
class DesktopSession:
    port: int
    expires_at: float
    container_name: str = "dockbench"
    used: bool = False


@dataclass
class TerminalSession:
    container_name: str
    expires_at: float
    used: bool = False


class DesktopSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, DesktopSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, port: int, container_name: str = "dockbench") -> str:
        async with self._lock:
            self._purge()
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = DesktopSession(
                port, time.monotonic() + SESSION_TTL_SECONDS, container_name
            )
            return session_id

    async def consume(self, session_id: str) -> DesktopSession | None:
        async with self._lock:
            self._purge()
            session = self._sessions.get(session_id)
            if session is None or session.used:
                return None
            session.used = True
            return session

    def _purge(self) -> None:
        now = time.monotonic()
        self._sessions = {
            key: value
            for key, value in self._sessions.items()
            if value.expires_at > now and not value.used
        }


class TerminalSessions:
    """Short-lived capabilities so terminal WebSockets cannot name arbitrary containers."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, container_name: str) -> str:
        async with self._lock:
            self._purge()
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = TerminalSession(
                container_name, time.monotonic() + SESSION_TTL_SECONDS
            )
            return session_id

    async def consume(self, session_id: str) -> TerminalSession | None:
        async with self._lock:
            self._purge()
            session = self._sessions.get(session_id)
            if session is None or session.used:
                return None
            session.used = True
            return session

    def _purge(self) -> None:
        now = time.monotonic()
        self._sessions = {
            key: value
            for key, value in self._sessions.items()
            if value.expires_at > now and not value.used
        }
