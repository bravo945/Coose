"""Live connections: which characters have a player watching right now, and
pushing "something changed" to their chatrooms.

Each open play page holds one WebSocket. The server only ever sends the word
"update"; the page then fetches its fresh chat. Request handlers run in a
thread pool, so they hand sends to the event loop that owns each socket.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass

from starlette.websockets import WebSocket


@dataclass(eq=False)
class _Conn:
    ws: WebSocket
    loop: asyncio.AbstractEventLoop
    username: str


class Hub:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._conns: dict[tuple[str, int], set[_Conn]] = defaultdict(set)

    def add(self, story: str, character_id: int, ws: WebSocket, username: str) -> _Conn:
        conn = _Conn(ws, asyncio.get_running_loop(), username)
        with self._guard:
            self._conns[(story, character_id)].add(conn)
        return conn

    def remove(self, story: str, character_id: int, conn: _Conn) -> None:
        with self._guard:
            conns = self._conns.get((story, character_id))
            if conns is not None:
                conns.discard(conn)
                if not conns:
                    del self._conns[(story, character_id)]

    def active(self, story: str, character_id: int) -> bool:
        """True while at least one of the character's players has the game open."""
        with self._guard:
            return bool(self._conns.get((story, character_id)))

    def online_users(self, story: str, character_id: int) -> list[str]:
        with self._guard:
            return sorted({c.username for c in self._conns.get((story, character_id), ())}, key=str.lower)

    def notify(self, story: str, character_ids) -> None:
        """Tell these characters' open pages to refresh. Safe from any thread."""
        with self._guard:
            targets = [c for cid in set(character_ids) for c in self._conns.get((story, cid), ())]
        self._send(targets)

    def notify_story(self, story: str, exclude: _Conn | None = None) -> None:
        """Refresh every open page in the story, e.g. when someone arrives or leaves."""
        with self._guard:
            targets = [c for (s, _), conns in self._conns.items() if s == story for c in conns if c is not exclude]
        self._send(targets)

    @staticmethod
    def _send(targets: list[_Conn]) -> None:
        for c in targets:
            if c.loop.is_closed():
                continue
            asyncio.run_coroutine_threadsafe(_safe_send(c.ws), c.loop)


async def _safe_send(ws: WebSocket) -> None:
    try:
        await ws.send_text("update")
    except Exception:  # the page went away mid-send; its own handler cleans up
        pass
