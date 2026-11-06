"""Bounded RFC 6455 text client. TLS verifies certificates; no redirects/proxies."""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

MAX_MESSAGE = 65536


class ProtocolError(ValueError):
    pass


def endpoint(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('wss', 'ws') or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.query
            or any(ord(c) < 33 or ord(c) > 126 for c in url)):
        raise ProtocolError('relay_invalid_url')
    if parsed.scheme == 'ws' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ProtocolError('relay_tls_required')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ProtocolError('relay_invalid_port')
    return parsed


class WebSocket:
    def __init__(self, sock, *, client=True):
        self.sock = sock
        self.client = client
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragmented = False
        self.close_sent = False
        self.close_received = False

    @classmethod
    def connect(cls, url, secret):
        parsed = endpoint(url)
        sock = socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == 'wss' else 80)), timeout=10)
        try:
            if parsed.scheme == 'wss':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            host = parsed.netloc
            sock.sendall((f'GET {parsed.path or "/"} HTTP/1.1\r\nHost: {host}\r\n'
                          'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                          f'Sec-WebSocket-Key: {key}\r\nAuthorization: Bearer {secret}\r\n\r\n').encode('ascii'))
            response = bytearray()
            while b'\r\n\r\n' not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ProtocolError('relay_upgrade_closed')
                response.extend(chunk)
                if len(response) > 16384:
                    raise ProtocolError('relay_upgrade_too_large')
            header, extra = bytes(response).split(b'\r\n\r\n', 1)
            try:
                lines = header.decode('ascii').split('\r\n')
                status = lines[0].split()
                if len(status) < 2 or status[0] != 'HTTP/1.1' or not status[1].isdigit():
                    raise ValueError()
                code = status[1]
                headers = {}
                for line in lines[1:]:
                    name, value = line.split(':', 1)
                    headers[name.lower()] = value.strip()
            except (ValueError, IndexError):
                raise ProtocolError('relay_invalid_upgrade') from None
            if code in ('401', '403'):
                raise ProtocolError('relay_auth_failed')
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if (code != '101' or headers.get('sec-websocket-accept') != accept
                    or headers.get('upgrade', '').lower() != 'websocket'
                    or 'upgrade' not in [v.strip().lower() for v in headers.get('connection', '').split(',')]
                    or 'sec-websocket-extensions' in headers):
                raise ProtocolError('relay_invalid_upgrade')
            ws = cls(sock)
            ws.buffer.extend(extra)
            sock.settimeout(1)
            return ws
        except BaseException:
            sock.close()
            raise

    def close(self, *, graceful=False, timeout=1.0):
        try:
            if graceful:
                deadline = time.monotonic() + timeout
                self.sock.settimeout(timeout)
                if not self.close_sent:
                    self.send_frame(struct.pack('!H', 1000), 8)
                while not self.close_received:
                    self.receive(deadline=deadline)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.sock.close()

    def send_frame(self, data, opcode=1):
        if len(data) > MAX_MESSAGE:
            raise ProtocolError('relay_frame_too_large')
        mask = os.urandom(4) if self.client else b''
        n = len(data)
        header = bytes([0x80 | opcode, (0x80 if mask else 0) | (n if n < 126 else 126 if n < 65536 else 127)])
        if n >= 126:
            header += struct.pack('!H' if n < 65536 else '!Q', n)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if mask else data
        self.sock.sendall(header + mask + payload)
        if opcode == 8:
            self.close_sent = True

    def send(self, value):
        self.send_frame(json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode())

    def receive(self, *, deadline=None):
        # Retain incomplete wire frames across socket timeouts. A timeout never
        # consumes half a header or forgets a fragmented message.
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise socket.timeout('relay_close_timeout')
            while len(self.buffer) < 2:
                self._more(deadline)
            first, second = self.buffer[:2]
            fin, opcode, masked = bool(first & 128), first & 15, bool(second & 128)
            n, pos = second & 127, 2
            if first & 112 or masked == self.client:
                raise ProtocolError('relay_invalid_frame')
            size = 2 if n == 126 else 8 if n == 127 else 0
            while len(self.buffer) < pos + size:
                self._more(deadline)
            if size:
                n = int.from_bytes(self.buffer[pos:pos + size], 'big')
                if n < (126 if size == 2 else 65536):
                    raise ProtocolError('relay_nonminimal_length')
                pos += size
            if n > MAX_MESSAGE or (opcode >= 8 and (n > 125 or not fin)):
                raise ProtocolError('relay_frame_too_large_or_control')
            end = pos + (4 if masked else 0) + n
            while len(self.buffer) < end:
                self._more(deadline)
            mask = self.buffer[pos:pos + 4] if masked else b''
            pos += len(mask)
            data = bytes(self.buffer[pos:end])
            del self.buffer[:end]
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                if len(data) == 1:
                    raise ProtocolError('relay_invalid_close')
                if data:
                    code = int.from_bytes(data[:2], 'big')
                    if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                        raise ProtocolError('relay_invalid_close')
                    try:
                        data[2:].decode('utf-8')
                    except UnicodeError:
                        raise ProtocolError('relay_invalid_close') from None
                self.close_received = True
                if not self.close_sent:
                    self.send_frame(data, 8)
                raise EOFError('relay_closed')
            if opcode == 9:
                self.send_frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not self.fragmented:
                self.fragments = bytearray(data)
            elif opcode == 0 and self.fragmented:
                self.fragments.extend(data)
            else:
                raise ProtocolError('relay_expected_text')
            if len(self.fragments) > MAX_MESSAGE:
                raise ProtocolError('relay_message_too_large')
            self.fragmented = not fin
            if fin:
                try:
                    return json.loads(self.fragments.decode('utf-8'))
                except (UnicodeError, ValueError, RecursionError):
                    raise ProtocolError('relay_invalid_json') from None

    def _more(self, deadline=None):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('relay_close_timeout')
            self.sock.settimeout(remaining)
        chunk = self.sock.recv(4096)
        if not chunk:
            raise EOFError('relay_closed')
        self.buffer.extend(chunk)
