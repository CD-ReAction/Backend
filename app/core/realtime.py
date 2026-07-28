"""
realtime.py
───────────
WebSocket 실시간 이벤트 브로드캐스트 (인메모리, 단일 uvicorn 워커 전제).

프론트 계약:
  클라이언트 → 서버:
    {"type": "subscribe",   "scope": {"project_id": 1, "session_id": 10}}
    {"type": "unsubscribe", "scope": {"project_id": 1, "session_id": 10}}
    {"type": "ping"}
  서버 → 클라이언트:
    {"type": "<event>", "scope": {...}, "payload": {...}, "server_sent_at": "...Z"}

스코프 매칭: 구독 스코프에서 null이 아닌 필드가 이벤트 스코프와 전부 일치하면 전달.
(project_id만 구독하면 그 프로젝트의 모든 세션 이벤트를 받음)

주의: 워커를 늘리면 인메모리 dict가 워커별로 분리되므로
Redis pub/sub 또는 Postgres LISTEN/NOTIFY로 교체 필요.
"""

import asyncio
import logging
from datetime import datetime

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# (project_id, session_id) — 둘 중 하나는 None일 수 있음
Scope = tuple[int | None, int | None]


def parse_scope(raw) -> Scope | None:
    """클라이언트가 보낸 scope dict → 내부 튜플. 유효한 id가 하나도 없으면 None."""
    if not isinstance(raw, dict):
        return None

    def _as_int(v):
        if v is None or isinstance(v, bool):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    project_id = _as_int(raw.get("project_id"))
    session_id = _as_int(raw.get("session_id"))
    if project_id is None and session_id is None:
        return None
    return (project_id, session_id)


class _Connection:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.scopes: set[Scope] = set()
        # 여러 REST 핸들러가 동시에 같은 소켓으로 send하지 않도록 직렬화
        self.lock = asyncio.Lock()


class RealtimeManager:
    def __init__(self):
        self._connections: dict[WebSocket, _Connection] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[ws] = _Connection(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)

    def subscribe(self, ws: WebSocket, scope: Scope) -> None:
        conn = self._connections.get(ws)
        if conn:
            conn.scopes.add(scope)

    def unsubscribe(self, ws: WebSocket, scope: Scope) -> None:
        conn = self._connections.get(ws)
        if conn:
            conn.scopes.discard(scope)

    async def send_to(self, ws: WebSocket, message: dict) -> None:
        """단일 연결로 전송 (pong 응답 등). 실패해도 예외를 올리지 않음."""
        conn = self._connections.get(ws)
        if not conn:
            return
        try:
            async with conn.lock:
                await conn.ws.send_json(message)
        except Exception:
            self.disconnect(ws)

    async def broadcast(
        self,
        event_type: str,
        project_id: int | None,
        session_id: int | None,
        payload: dict,
    ) -> None:
        """스코프가 일치하는 모든 연결로 이벤트 전송.

        REST 핸들러 안에서 commit 후 호출되므로 어떤 경우에도 예외를 올리지 않는다.
        전송 실패한 연결은 죽은 것으로 보고 제거.
        """
        message = {
            "type": event_type,
            "scope": {"project_id": project_id, "session_id": session_id},
            "payload": payload,
            "server_sent_at": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        }
        for conn in list(self._connections.values()):
            if not self._matches(conn, project_id, session_id):
                continue
            try:
                async with conn.lock:
                    await conn.ws.send_json(message)
            except Exception:
                logger.warning("realtime send 실패 — 연결 제거", exc_info=True)
                self.disconnect(conn.ws)

    @staticmethod
    def _matches(conn: _Connection, project_id: int | None, session_id: int | None) -> bool:
        """구독 scope와 이벤트 scope 비교.

        어느 한쪽 필드가 None이면 그 필드는 와일드카드:
        - 구독이 project만 지정 → 그 프로젝트의 모든 세션 이벤트 수신
        - 이벤트가 project 레벨(session_id=None, 예: actor.*) → 그 프로젝트의
          모든 세션 구독자에게 전달
        """
        for sub_project, sub_session in conn.scopes:
            if (
                sub_project is not None
                and project_id is not None
                and sub_project != project_id
            ):
                continue
            if (
                sub_session is not None
                and session_id is not None
                and sub_session != session_id
            ):
                continue
            return True
        return False


manager = RealtimeManager()
