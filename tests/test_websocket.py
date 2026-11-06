import json
import socket
import struct
import unittest
from unittest.mock import patch
import threading
import time
from coordkit.websocket import WebSocket, ProtocolError


class WireTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(0.02)
        return WebSocket(a), b

    def test_masking_lengths_fragmented_text_ping_and_partial_timeout(self):
        client, wire = self.pair()
        client.send({'text': 'x' * 200})
        received = wire.recv(1024)
        self.assertTrue(received[1] & 128)
        length = struct.unpack('!H', received[2:4])[0]
        mask = received[4:8]
        decoded = bytes(v ^ mask[i % 4] for i, v in enumerate(received[8:]))
        self.assertEqual(len(decoded), length)
        self.assertEqual(json.loads(decoded)['text'], 'x' * 200)
        wire.sendall(b'\x01\x05{"a":' + b'\x89\x01p' + b'\x80')
        with self.assertRaises(socket.timeout):
            client.receive()
        self.assertEqual(wire.recv(1024)[0], 0x8a)
        wire.sendall(b'\x02' + b'1}')
        self.assertEqual(client.receive(), {'a': 1})

    def test_reject_masked_server_huge_binary_and_invalid_json(self):
        for payload in (b'\x81\x80', b'\x81\x7f' + (65537).to_bytes(8, 'big'), b'\x82\x00', b'\x81\x01x'):
            with self.subTest(payload=payload):
                client, wire = self.pair()
                wire.sendall(payload)
                with self.assertRaises(ProtocolError):
                    client.receive()

    def test_close_is_acknowledged_and_invalid_close_or_lengths_rejected(self):
        client, wire = self.pair()
        wire.sendall(b'\x88\x02\x03\xe8')
        with self.assertRaises(EOFError):
            client.receive()
        reply = wire.recv(1024)
        self.assertEqual(reply[0], 0x88)
        self.assertTrue(reply[1] & 128)
        for payload in (b'\x88\x01x', b'\x88\x02\x03\xed', b'\x81\x7e\x00\x01'):
            client, wire = self.pair()
            wire.sendall(payload)
            with self.assertRaises(ProtocolError):
                client.receive()

    def test_malformed_upgrade_status_and_headers_are_protocol_errors(self):
        for response in (b'garbage', b'HTTP/1.1', b'HTTP/1.1 101 Switching\r\nmissing-colon', b'\xff'):
            fake = unittest.mock.Mock()
            fake.recv.return_value = response + b'\r\n\r\n'
            with self.subTest(response=response), patch('socket.create_connection', return_value=fake):
                with self.assertRaises(ProtocolError):
                    WebSocket.connect('ws://127.0.0.1/bus/test', 'test')
                fake.close.assert_called_once()

    def test_deep_json_is_a_protocol_error(self):
        client, wire = self.pair()
        payload = b'[' * 2000 + b'0' + b']' * 2000
        wire.sendall(b'\x81\x7e' + len(payload).to_bytes(2, 'big') + payload)
        with self.assertRaises(ProtocolError):
            client.receive()

    def test_normal_shutdown_sends_masked_close_and_waits_for_peer(self):
        client, wire = self.pair()
        observed = []
        def peer():
            received = wire.recv(1024)
            observed.append(received)
            time.sleep(0.03)
            wire.sendall(b'\x88\x02\x03\xe8')
        thread = threading.Thread(target=peer)
        thread.start()
        client.close(graceful=True, timeout=0.5)
        thread.join(1)
        self.assertTrue(client.close_received)
        frame = observed[0]
        self.assertEqual(frame[:2], b'\x88\x82')
        self.assertEqual(bytes(b ^ frame[2+i] for i, b in enumerate(frame[6:])), b'\x03\xe8')

    def test_shutdown_has_a_hard_deadline_when_peer_does_not_close(self):
        client, wire = self.pair()
        start = time.monotonic()
        client.close(graceful=True, timeout=0.04)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(wire.recv(1024)[0], 0x88)
