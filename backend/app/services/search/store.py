"""
In-memory search sessions. Each search step (first page, more) runs in a
background thread; at most MAX_CONCURRENT_SEARCHES run at once because each
one launches a browser. Sessions expire after SESSION_TTL_S.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable

from app.services.search.agent import SearchAgent, SearchFailed, SearchSession

logger = logging.getLogger("VideoSearch")

SESSION_TTL_S = 3600
MAX_CONCURRENT_SEARCHES = 2


class SearchBusy(Exception):
    """The search is still working on its previous step."""


def _run_in_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="video-search", daemon=True).start()


class SearchStore:
    def __init__(
        self,
        agent_factory: Callable[[], SearchAgent],
        run_in_background: Callable[[Callable[[], None]], None] = _run_in_thread,
        ttl_s: float = SESSION_TTL_S,
        max_concurrent: int = MAX_CONCURRENT_SEARCHES,
    ) -> None:
        self._agent_factory = agent_factory
        self._run_in_background = run_in_background
        self._ttl_s = ttl_s
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._sessions: dict[str, SearchSession] = {}
        self._lock = threading.Lock()

    def start(self, description: str) -> SearchSession:
        session = SearchSession(search_id=uuid.uuid4().hex, description=description, created_at=time.time())
        with self._lock:
            self._drop_expired_locked()
            self._sessions[session.search_id] = session
        self._launch(session, "first_page")
        return session

    def get(self, search_id: str) -> SearchSession | None:
        with self._lock:
            return self._sessions.get(search_id)

    def more(self, search_id: str) -> SearchSession:
        with self._lock:
            session = self._sessions.get(search_id)
            if session is None:
                raise KeyError(search_id)
            if session.status == "running":
                raise SearchBusy(search_id)
            session.status = "running"
        self._launch(session, "more")
        return session

    def _drop_expired_locked(self) -> None:
        cutoff = time.time() - self._ttl_s
        for search_id in [sid for sid, s in self._sessions.items() if s.created_at < cutoff]:
            del self._sessions[search_id]

    def _launch(self, session: SearchSession, step: str) -> None:
        def target() -> None:
            with self._slots:
                try:
                    asyncio.run(getattr(self._agent_factory(), step)(session))
                    session.status = "done"
                except SearchFailed as exc:
                    session.error = str(exc)
                    session.status = "failed"
                except Exception:
                    logger.exception(f"Search {session.search_id} crashed during {step}")
                    session.error = "Search failed unexpectedly. Check the backend log."
                    session.status = "failed"
                finally:
                    session.phase = None

        self._run_in_background(target)
