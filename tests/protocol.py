"""Exercise the installed Nginx binary through TLS and HTTP/2 wire traffic."""

import asyncio
import hashlib
import os
import ssl
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import h2.config
import h2.connection
import h2.events
import h2.settings

NGINX_BINARY = os.environ.get("NGINX_BINARY", "nginx")
EXPECTED = int(os.environ.get("EXPECTED_FRAME_SIZE", "16384"))
LARGE = b"cresset-stream-data\n" * 131072
UPLOAD_PROGRESS = 0


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def http_backend(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    global UPLOAD_PROGRESS
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split(b" ")[1]
        if path == b"/disconnect":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\npartial")
            await writer.drain()
            return
        if path == b"/upload":
            digest = hashlib.sha256()
            size = 0
            headers = head.lower()
            if b"transfer-encoding: chunked" in headers:
                while True:
                    length = int((await reader.readline()).strip(), 16)
                    if length == 0:
                        await reader.readline()
                        break
                    chunk = await reader.readexactly(length)
                    await reader.readexactly(2)
                    digest.update(chunk)
                    size += length
                    UPLOAD_PROGRESS = size
            else:
                length = int(
                    next(
                        line.split(b":", 1)[1]
                        for line in headers.split(b"\r\n")
                        if line.startswith(b"content-length:")
                    )
                )
                while size < length:
                    chunk = await reader.read(min(8192, length - size))
                    require(bool(chunk), "Upload upstream closed before body completed")
                    digest.update(chunk)
                    size += len(chunk)
                    UPLOAD_PROGRESS = size
            body = f"{size}:{digest.hexdigest()}".encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            for offset in range(0, len(LARGE), 8192):
                chunk = LARGE[offset : offset + 8192]
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0.002)
            writer.write(b"0\r\n\r\n")
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def grpc_backend(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    conn = h2.connection.H2Connection(
        config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
    )
    conn.initiate_connection()
    writer.write(conn.data_to_send())
    bodies: dict[int, bytearray] = {}
    paths: dict[int, str] = {}
    pending: dict[int, bytes] = {}
    try:
        while data := await reader.read(65536):
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    bodies[event.stream_id] = bytearray()
                    paths[event.stream_id] = dict(event.headers)[":path"]
                elif isinstance(event, h2.events.DataReceived):
                    bodies[event.stream_id].extend(event.data)
                    conn.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id
                    )
                elif isinstance(event, h2.events.StreamEnded):
                    sid = event.stream_id
                    if paths[sid] == "/grpc.Disconnect/Call":
                        return
                    conn.send_headers(
                        sid, [(":status", "200"), ("content-type", "application/grpc")]
                    )
                    pending[sid] = (
                        bytes(bodies.pop(sid))
                        or b"\x00" + struct.pack("!I", len(LARGE)) + LARGE
                    )
            for sid in list(pending):
                remaining = pending[sid]
                while remaining and conn.local_flow_control_window(sid) > 0:
                    length = min(
                        len(remaining),
                        conn.local_flow_control_window(sid),
                        conn.max_outbound_frame_size,
                    )
                    conn.send_data(sid, remaining[:length])
                    remaining = remaining[length:]
                pending[sid] = remaining
                if not remaining:
                    conn.send_headers(sid, [("grpc-status", "0")], end_stream=True)
                    del pending[sid]
            writer.write(conn.data_to_send())
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@dataclass
class Response:
    headers: dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    reset: int | None = None


class Client:
    def __init__(self) -> None:
        self.conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(header_encoding="utf-8")
        )
        self.responses: dict[int, Response] = {}
        self.settings = asyncio.Event()
        self.credit = asyncio.Event()
        self.goaways: list[int] = []
        self.failure: Exception | None = None
        self.slow = False
        self.explicit_frame_setting = False

    async def connect(self) -> None:
        ctx = ssl.create_default_context(cafile="/tmp/test-nginx/cert.pem")
        ctx.set_alpn_protocols(["h2"])
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", 8443, ssl=ctx, server_hostname="localhost"
        )
        require(
            self.writer.get_extra_info("ssl_object").selected_alpn_protocol() == "h2",
            "ALPN did not negotiate h2",
        )
        self.conn.initiate_connection()
        self.conn.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 32768})
        self.flush()
        self.task = asyncio.create_task(self.receive())
        await asyncio.wait_for(self.settings.wait(), 10)
        require(self.explicit_frame_setting, "Server omitted SETTINGS_MAX_FRAME_SIZE")
        require(
            self.conn.remote_settings.max_frame_size == EXPECTED,
            f"SETTINGS_MAX_FRAME_SIZE: {self.conn.remote_settings.max_frame_size}, expected {EXPECTED}",
        )

    def flush(self) -> None:
        self.writer.write(self.conn.data_to_send())

    async def receive(self) -> None:
        try:
            while data := await self.reader.read(65536):
                for event in self.conn.receive_data(data):
                    if isinstance(event, h2.events.RemoteSettingsChanged):
                        self.explicit_frame_setting |= (
                            h2.settings.SettingCodes.MAX_FRAME_SIZE
                            in event.changed_settings
                        )
                        self.settings.set()
                    elif isinstance(
                        event, (h2.events.ResponseReceived, h2.events.TrailersReceived)
                    ):
                        self.responses[event.stream_id].headers.update(
                            dict(event.headers)
                        )
                    elif isinstance(event, h2.events.DataReceived):
                        self.responses[event.stream_id].body.extend(event.data)
                        if self.slow:
                            await asyncio.sleep(0.003)
                        self.conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, h2.events.StreamEnded):
                        self.responses[event.stream_id].done.set()
                    elif isinstance(event, h2.events.StreamReset):
                        self.responses[event.stream_id].reset = int(event.error_code)
                        self.responses[event.stream_id].done.set()
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        self.goaways.append(int(event.error_code))
                    elif isinstance(event, h2.events.WindowUpdated):
                        self.credit.set()
                self.flush()
        # Relay background reader failures to the request awaiting this connection.
        except Exception as exc:  # noqa: BLE001
            self.failure = exc
        finally:
            for response in self.responses.values():
                response.done.set()

    async def request(
        self, path: str, body: bytes | None = None, streaming: bool = False
    ) -> Response:
        sid = self.conn.get_next_available_stream_id()
        response = Response()
        self.responses[sid] = response
        self.conn.send_headers(
            sid,
            [
                (":method", "POST" if body is not None else "GET"),
                (":scheme", "https"),
                (":authority", "localhost"),
                (":path", path),
                (
                    "content-type",
                    "application/grpc"
                    if path.startswith("/grpc")
                    else "application/octet-stream",
                ),
            ],
            end_stream=body is None,
        )
        self.flush()
        if body is not None:
            offset = 0
            while offset < len(body):
                self.credit.clear()
                length = min(
                    len(body) - offset,
                    self.conn.local_flow_control_window(sid),
                    self.conn.max_outbound_frame_size,
                )
                if length <= 0:
                    await asyncio.wait_for(self.credit.wait(), 10)
                    continue
                self.conn.send_data(
                    sid,
                    body[offset : offset + length],
                    end_stream=offset + length == len(body),
                )
                offset += length
                self.flush()
                await self.writer.drain()
                if streaming and offset >= 65536 and offset - length < 65536:
                    for _ in range(100):
                        if UPLOAD_PROGRESS > 0:
                            break
                        await asyncio.sleep(0.01)
                    require(
                        UPLOAD_PROGRESS > 0,
                        "proxy_request_buffering off did not forward the unfinished upload",
                    )
                await asyncio.sleep(0)
        await asyncio.wait_for(response.done.wait(), 60)
        if self.failure:
            raise self.failure
        require(not self.goaways, f"Unexpected GOAWAY: {self.goaways}")
        return response

    async def close(self) -> None:
        self.conn.close_connection()
        self.flush()
        await self.writer.drain()
        self.writer.close()
        await self.writer.wait_closed()
        await self.task
        require(self.failure is None, f"HTTP/2 reader failed: {self.failure}")


def check(response: Response, body: bytes, grpc: bool = False) -> None:
    require(response.reset is None, f"Unexpected RST_STREAM: {response.reset}")
    require(
        response.headers.get(":status") == "200",
        f"Unexpected response: {response.headers}",
    )
    require(response.body == body, "Response payload differs from upstream")
    if grpc:
        require(
            response.headers.get("grpc-status") == "0",
            "Missing successful gRPC trailers",
        )


async def oversized() -> None:
    client = Client()
    await client.connect()
    # The first frame header alone must trigger FRAME_SIZE_ERROR (6), before its payload arrives.
    client.writer.write((16385).to_bytes(3, "big") + b"\xff\x00\x00\x00\x00\x00")
    await client.writer.drain()
    for _ in range(100):
        if client.goaways:
            break
        await asyncio.sleep(0.01)
    require(
        client.goaways == [6],
        f"Oversized frame did not produce GOAWAY FRAME_SIZE_ERROR: {client.goaways}",
    )
    client.writer.close()
    await client.writer.wait_closed()
    await client.task
    print(
        "PASS oversized unknown frame rejected with GOAWAY FRAME_SIZE_ERROR (6)",
        flush=True,
    )


async def main() -> None:
    root = Path("/tmp/test-nginx")
    root.mkdir()
    await asyncio.to_thread(
        subprocess.run,
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(root / "key.pem"),
            "-out",
            str(root / "cert.pem"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    http = await asyncio.start_server(http_backend, "127.0.0.1", 8081)
    grpc_tcp = await asyncio.start_server(grpc_backend, "127.0.0.1", 8082)
    grpc_unix = await asyncio.start_unix_server(
        grpc_backend, path=str(root / "grpc.sock")
    )
    os.chmod(root / "grpc.sock", 0o777)
    modules = "\n".join(
        f"load_module {path};"
        for path in sorted(Path("/usr/lib/nginx/modules").glob("*.so"))
        if ("-debug" in path.name) == NGINX_BINARY.endswith("-debug")
    )
    conf = f"""{modules}
user nginx;
worker_processes 1;
pid /tmp/test-nginx/nginx.pid;
error_log /tmp/test-nginx/error.log info;
events {{ worker_connections 512; }}
http {{
    access_log off;
    client_max_body_size 32m;
    server {{
        listen 8443 ssl;
        http2 on;
        server_name localhost;
        ssl_certificate /tmp/test-nginx/cert.pem;
        ssl_certificate_key /tmp/test-nginx/key.pem;
        location = / {{ return 200 'ok'; }}
        location /grpc.Unix/ {{ grpc_pass grpc://unix:/tmp/test-nginx/grpc.sock; }}
        location /grpc {{ grpc_pass grpc://127.0.0.1:8082; }}
        location / {{
            proxy_pass http://127.0.0.1:8081;
            proxy_http_version 1.1;
            proxy_request_buffering off;
            proxy_buffering off;
        }}
    }}
}}
"""
    (root / "nginx.conf").write_text(conf)
    await asyncio.to_thread(
        subprocess.run, [NGINX_BINARY, "-t", "-c", str(root / "nginx.conf")], check=True
    )
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [NGINX_BINARY, "-c", str(root / "nginx.conf"), "-g", "daemon off;"],
    )
    started = time.monotonic()
    try:
        for _ in range(100):
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", 8443)
                writer.close()
                await writer.wait_closed()
                break
            except ConnectionRefusedError:
                await asyncio.sleep(0.05)
        client = Client()
        await client.connect()
        print(
            f"PASS TLS ALPN h2; SETTINGS_MAX_FRAME_SIZE: {client.conn.remote_settings.max_frame_size}",
            flush=True,
        )
        check(await client.request("/"), b"ok")
        print("PASS GET and all shipped dynamic module ABI checks", flush=True)
        upload = b"upload-data\x00" * 262144
        check(
            await client.request("/upload", upload, streaming=True),
            f"{len(upload)}:{hashlib.sha256(upload).hexdigest()}".encode(),
        )
        print(
            f"PASS streamed upload: {len(upload)} bytes forwarded before END_STREAM",
            flush=True,
        )
        client.slow = True
        grpc_body = b"\x00" + struct.pack("!I", len(upload)) + upload
        requests = [
            client.request("/stream"),
            client.request("/grpc.TCP/Call", grpc_body),
            client.request("/grpc.Unix/Call", grpc_body),
        ] + [client.request("/") for _ in range(12)]
        results = await asyncio.gather(*requests)
        check(results[0], LARGE)
        check(results[1], grpc_body, grpc=True)
        check(results[2], grpc_body, grpc=True)
        for result in results[3:]:
            check(result, b"ok")
        print(
            "PASS 15 concurrent streams, large streaming response, TCP/Unix gRPC, trailers, small windows and slow consumer",
            flush=True,
        )
        client.slow = False
        disconnected = await client.request("/disconnect")
        require(
            disconnected.reset == 2,
            f"Expected isolated INTERNAL_ERROR on truncated upstream, got {disconnected}",
        )
        failed_grpc = await client.request(
            "/grpc.Disconnect/Call", b"\x00\x00\x00\x00\x00"
        )
        require(
            failed_grpc.headers.get(":status") == "502" and failed_grpc.reset is None,
            f"Expected gRPC upstream failure HTTP 502, got {failed_grpc}",
        )
        check(await client.request("/"), b"ok")
        print(
            "PASS upstream disconnects isolated; subsequent stream succeeds; no unexpected RST_STREAM/GOAWAY/PROTOCOL_ERROR",
            flush=True,
        )
        await client.close()
        print("PASS clean connection close", flush=True)
        if EXPECTED == 16384:
            await oversized()
        print(
            f"PASS all protocol tests in {time.monotonic() - started:.1f}s", flush=True
        )
    finally:
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait)
        http.close()
        grpc_tcp.close()
        grpc_unix.close()
        print((root / "error.log").read_text(), flush=True)
        require(proc.returncode == 0, f"Nginx exited with {proc.returncode}")


if __name__ == "__main__":
    asyncio.run(main())
