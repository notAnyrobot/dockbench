"""PTY transport ownership, from allocation through child reaping.

A cancelled coroutine does not stop a blocking reader thread or a spawned
process. Use event-loop descriptor readiness and explicitly settle launch before
closing descriptors, then terminate and reap the child on every exit path.
"""

from __future__ import annotations

import asyncio
import anyio
import fcntl
import os
import pty
import struct
import termios
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class Terminal:
    def __init__(self, master: int, process: asyncio.subprocess.Process) -> None:
        self.master = master
        self.process = process

    def resize(self, rows: int, columns: int) -> None:
        if 1 <= rows <= 1000 and 1 <= columns <= 1000:
            fcntl.ioctl(
                self.master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, 0, 0),
            )

    async def _ready(self, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def wake() -> None:
            if not ready.done():
                ready.set_result(None)

        add = loop.add_writer if write else loop.add_reader
        remove = loop.remove_writer if write else loop.remove_reader
        add(self.master, wake)
        try:
            await ready
        finally:
            remove(self.master)

    async def read(self) -> bytes:
        while True:
            await self._ready()
            try:
                return os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return b""

    async def write(self, data: str) -> None:
        pending = memoryview(data.encode())
        while pending:
            try:
                size = os.write(self.master, pending)
                pending = pending[size:]
            except BlockingIOError:
                await self._ready(write=True)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


@asynccontextmanager
async def open_terminal(
    command: list[str], environment: dict[str, str], rows: int, columns: int
) -> AsyncIterator[Terminal]:
    master, slave = pty.openpty()
    process = None
    launch = None
    try:
        fcntl.ioctl(
            master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
        )
        os.set_blocking(master, False)

        def attach() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=environment,
                preexec_fn=attach,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Launch may still complete after the request is gone. Adopt its
            # child so the finally block can reap it instead of orphaning it.
            with anyio.CancelScope(shield=True):
                try:
                    process = await launch
                except (asyncio.CancelledError, OSError):
                    pass
            raise
        os.close(slave)
        slave = -1
        yield Terminal(master, process)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
        if process is not None:
            # ASGI cancellation scopes keep cancelling subsequent awaits;
            # protect reaping from that scope as well as task cancellation.
            with anyio.CancelScope(shield=True):
                cleanup = asyncio.create_task(_stop(process))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
