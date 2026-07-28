"""
ws.py
─────
프론트 실시간 레이어용 WebSocket 엔드포인트.

프론트는 VITE_WS_URL(예: ws://localhost:8000/ws)로 접속한 뒤
subscribe / unsubscribe / ping 메시지를 보낸다.
이벤트 broadcast는 각 REST 핸들러가 app.core.realtime.manager를 통해 수행.
"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.realtime import manager, parse_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])


@router.websocket("/ws")
async def realtime_ws(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # JSON이 아니거나 binary 프레임 → 무시
                continue

            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")

            if msg_type == "subscribe":
                scope = parse_scope(msg.get("scope"))
                if scope:
                    manager.subscribe(ws, scope)
            elif msg_type == "unsubscribe":
                scope = parse_scope(msg.get("scope"))
                if scope:
                    manager.unsubscribe(ws, scope)
            elif msg_type == "ping":
                await manager.send_to(ws, {"type": "pong"})
            # 그 외 타입은 무시 (프로토콜 확장 대비)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
