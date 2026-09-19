"""mDNS advertising of the UbuDesk service (`_ubudesk._tcp.local.`)."""

from __future__ import annotations

import logging
import socket
from typing import Any

log = logging.getLogger(__name__)

SERVICE_TYPE = "_ubudesk._tcp.local."


class Advertiser:
    def __init__(self, port: int, server_id: str, name: str):
        self._port = port
        self._server_id = server_id
        self._name = name
        self._zc: Any = None
        self._info: Any = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.warning("zeroconf not installed; mDNS discovery disabled")
            return
        try:
            addresses = _local_addresses()
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self._name}.{SERVICE_TYPE}",
                addresses=addresses,
                port=self._port,
                properties={
                    "proto": "1",
                    "id": self._server_id,
                    "name": self._name,
                },
            )
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            log.info("mDNS: advertising %s on port %d", SERVICE_TYPE, self._port)
        except Exception as exc:  # noqa: BLE001 - discovery is optional; never fatal
            log.warning("mDNS advertising failed (%s); manual IP entry still works", exc)
            self._zc = None

    def stop(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:  # noqa: BLE001
                pass
            self._zc = None


def _local_addresses() -> list[bytes]:
    """Best-effort list of non-loopback IPv4 addresses."""
    addrs: list[bytes] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # no traffic is sent
            addrs.append(socket.inet_aton(s.getsockname()[0]))
        finally:
            s.close()
    except OSError:
        pass
    if not addrs:
        addrs.append(socket.inet_aton("127.0.0.1"))
    return addrs
