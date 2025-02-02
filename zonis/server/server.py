import json
import secrets
from typing import Dict, Literal, Any, cast, Optional

from websockets.exceptions import ConnectionClosedOK

from zonis import (
    Packet,
    UnknownClient,
    RequestFailed,
    BaseZonisException,
    DuplicateConnection,
)
from zonis.packet import RequestPacket, IdentifyPacket


class Server:
    """
    Parameters
    ----------
    using_fastapi_websockets: :class:`bool`
        Defaults to ``False``.
    override_key: Optional[:class:`str`]
    secret_key: :class:`str`
        Defaults to an emptry string.
    """
    def __init__(
        self,
        *,
        using_fastapi_websockets: bool = False,
        override_key: Optional[str] = None,
        secret_key: str = "",
    ) -> None:
        self._connections = {}
        self._secret_key: str = secret_key
        self._override_key: Optional[str] = (
            override_key if override_key is not None else secrets.token_hex(64)
        )
        self.using_fastapi_websockets: bool = using_fastapi_websockets

    def disconnect(self, identifier: str) -> None:
        self._connections.pop(identifier, None)

    async def _send(self, content: str, conn) -> None:
        if self.using_fastapi_websockets:
            await conn.send_text(content)
        else:
            await conn.send(content)

    async def _recv(self, conn) -> str:
        if self.using_fastapi_websockets:
            from starlette.websockets import WebSocketDisconnect

            try:
                return await conn.receive_text()
            except WebSocketDisconnect:
                raise RequestFailed("Websocket disconnected while waiting for receive.")

        return await conn.recv()

    async def request(
        self, route: str, *, client_identifier: str = "DEFAULT", **kwargs
    ):
        conn = self._connections.get(client_identifier)
        if not conn:
            raise UnknownClient

        await self._send(
            json.dumps(
                Packet(
                    identifier=client_identifier,
                    type="REQUEST",
                    data=RequestPacket(route=route, arguments=kwargs),
                )
            ),
            conn,
        )
        d = await self._recv(conn)
        packet: Packet = json.loads(d)
        if packet["type"] == "FAILURE_RESPONSE":
            raise RequestFailed(packet["data"])

        return packet["data"]

    async def request_all(self, route: str, **kwargs) -> Dict[str, Any]:
        results: Dict[str, Any] = {}

        for i, conn in self._connections.items():
            try:
                await self._send(
                    json.dumps(
                        Packet(
                            identifier=i,
                            type="REQUEST",
                            data=RequestPacket(route=route, arguments=kwargs),
                        )
                    ),
                    conn,
                )
                d = await self._recv(conn)
                packet: Packet = json.loads(d)
                if packet["type"] == "FAILURE_RESPONSE":
                    results[i] = RequestFailed(packet["data"])
                else:
                    results[i] = packet["data"]
            except ConnectionClosedOK:
                results[i] = RequestFailed("Connection Closed")

        return results

    async def parse_identify(self, packet: Packet, websocket) -> str:
        try:
            identifier: str = packet.get("identifier")
            ws_type: Literal["IDENTIFY"] = packet["type"]
            if ws_type != "IDENTIFY":
                await websocket.close(
                    code=4101, reason=f"Expected IDENTIFY, received {ws_type}"
                )
                raise BaseZonisException(
                    f"Unexpected ws response type, expected IDENTIFY, received {ws_type}"
                )

            packet: IdentifyPacket = cast(IdentifyPacket, packet)
            secret_key = packet["data"]["secret_key"]
            if secret_key != self._secret_key:
                await websocket.close(code=4100, reason=f"Invalid secret key.")
                raise BaseZonisException(
                    f"Client attempted to connect with an incorrect secret key."
                )

            override_key = packet["data"].get("override_key")
            if identifier in self._connections and (
                not override_key or override_key != self._override_key
            ):
                await websocket.close(
                    code=4102, reason="Duplicate identifier on IDENTIFY"
                )
                raise DuplicateConnection("Identify failed.")

            self._connections[identifier] = websocket
            await self._send(
                json.dumps(Packet(identifier=identifier, type="IDENTIFY", data=None)),
                websocket,
            )
            return identifier
        except Exception as e:
            raise BaseZonisException("Identify failed") from e
