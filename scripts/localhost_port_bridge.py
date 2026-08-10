from __future__ import annotations

import argparse
import selectors
import signal
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass


BUFFER_SIZE = 64 * 1024


@dataclass(frozen=True)
class BridgeMapping:
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        mapping = self.server.mapping
        upstream = socket.create_connection(
            (mapping.target_host, mapping.target_port),
            timeout=10.0,
        )
        upstream.setblocking(False)
        self.request.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(self.request, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, self.request)
        sockets = (self.request, upstream)
        try:
            while True:
                events = selector.select(timeout=1.0)
                if not events:
                    continue
                for key, _ in events:
                    source: socket.socket = key.fileobj
                    target: socket.socket = key.data
                    chunk = source.recv(BUFFER_SIZE)
                    if not chunk:
                        return
                    target.sendall(chunk)
        finally:
            selector.close()
            for item in sockets:
                try:
                    item.close()
                except OSError:
                    pass


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mapping: BridgeMapping):
        self.mapping = mapping
        super().__init__((mapping.listen_host, mapping.listen_port), _RelayHandler)


def _parse_mapping(text: str) -> BridgeMapping:
    listen, target = text.split("=", 1)
    listen_host, listen_port = listen.rsplit(":", 1)
    target_host, target_port = target.rsplit(":", 1)
    return BridgeMapping(
        listen_host=listen_host.strip(),
        listen_port=int(listen_port),
        target_host=target_host.strip(),
        target_port=int(target_port),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge localhost ports to WSL or other remote TCP ports.")
    parser.add_argument(
        "--map",
        action="append",
        required=True,
        metavar="LISTEN_HOST:LISTEN_PORT=TARGET_HOST:TARGET_PORT",
        help="One TCP bridge mapping. May be provided multiple times.",
    )
    args = parser.parse_args(argv)
    mappings = [_parse_mapping(item) for item in args.map]
    servers = [_ThreadedTCPServer(mapping) for mapping in mappings]
    threads = [
        threading.Thread(target=server.serve_forever, name=f"bridge-{server.mapping.listen_port}", daemon=True)
        for server in servers
    ]
    stop_event = threading.Event()

    def _shutdown(*_: object) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        for server in servers:
            server.shutdown()
            server.server_close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    for server, thread in zip(servers, threads, strict=True):
        thread.start()
        print(
            f"bridge_listening {server.mapping.listen_host}:{server.mapping.listen_port} -> "
            f"{server.mapping.target_host}:{server.mapping.target_port}",
            flush=True,
        )

    try:
        stop_event.wait()
    finally:
        _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
