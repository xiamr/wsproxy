#!/usr/bin/env python3

import asyncio
import websockets
import msgpack
import ipaddress
import socket
import logging
import concurrent.futures
import argparse
import json
from typing import *
from cipher_algorithm import *
import sys

class Stream:
    def __init__(self, reader, writer, atyp, remote, port, stream_id, local):

        self.reader = reader
        self.writer = writer
        self.atyp = atyp
        self.remote = remote
        self.port = port
        self.stream_id = stream_id
        self.local = local

        self.read_task = None
        self.write_task = None

        self.write_queue = asyncio.Queue()
        self.new_write_queue = asyncio.Event()
        self.new_write_queue.set()

        self.write_and_close = False

    async def run(self):
        data = pack({"METHOD": "CONNECT", "ATYP": self.atyp,
                     "REMOTE": self.remote, "PORT": self.port, "ID": self.stream_id})

        logging.info("start new stream :%s, %s:%s" % (self.stream_id, self.remote, self.port))

        await self.local.send_queue.put(data)

    def connection_established(self):
        self.writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        self.read_task = asyncio.ensure_future(self.read_from_client())
        self.write_task = asyncio.ensure_future(self.write_to_client())

    def connection_failure(self):
        self.writer.write(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
        self.writer.close()
        del self.local.stream_map[self.stream_id]

    async def read_from_client(self):
        while True:
            try:
                raw_data = await self.reader.read(8196)
            except ConnectionResetError:
                logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                await self.local.send_queue.put(pack({"METHOD": "RCLOSE", "ID": self.stream_id}))
                return
            if len(raw_data) == 0:
                if self.reader.at_eof():
                    logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                    await self.local.send_queue.put(pack({"METHOD": "RCLOSE", "ID": self.stream_id}))
                    return
            logging.debug("recv some data from client: stream %s" % self.stream_id)

            await self.local.send_queue.put(pack({"ID": self.stream_id, "DATA": raw_data}))

    async def write_to_client(self):
        while True:
            if not self.write_and_close:
                await self.new_write_queue.wait()
            try:
                message = self.write_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                self.new_write_queue.clear()
                if self.write_and_close:
                    logging.info("close writer : stream %s" % self.stream_id)
                    if not self.writer.transport.is_closing():
                        self.writer.write_eof()
                    return
                continue

            logging.debug("send some data to client: stream %s" % self.stream_id)
            self.writer.write(message["DATA"])
            try:
                await self.writer.drain()
            except concurrent.futures.CancelledError:
                raise
            except ConnectionResetError:
                if not self.writer.transport.is_closing():
                    self.writer.write_eof()
                return

    def rclose(self):
        logging.debug("prepare to close writer : stream %s" % self.stream_id)
        self.write_and_close = True
        self.new_write_queue.set()
        del self.local.stream_map[self.stream_id]


class Local:
    def __init__(self, cipher):
        self.ws = None
        self.stream_map = {}
        self.send_queue = asyncio.Queue()
        self.cipher = cipher

    async def send_to_peer(self):
        while True:
            data = await self.send_queue.get()
            await self.ws.send(self.cipher.encrypt(data))

    async def read_from_peer(self):
        while True:
            data = await self.ws.recv()
            data = self.cipher.decrypt(data)
            message = unpack(data)
            ID = message['ID']
            if 'METHOD' in message:
                if message['METHOD'] == 'RCLOSE':
                    if ID in self.stream_map:
                        self.stream_map[ID].rclose()
                elif message['METHOD'] == "CONNECTIONOK":
                    if ID in self.stream_map:
                        self.stream_map[ID].connection_established()
                elif message['METHOD'] == "CONNECTIONFAILURE":
                    if ID in self.stream_map:
                        self.stream_map[ID].connection_failure()
            else:
                logging.debug("recv data from peer : stream %s" % ID)
                if ID in self.stream_map:
                    await self.stream_map[ID].write_queue.put(message)
                    self.stream_map[ID].new_write_queue.set()

    async def connect_to_peer(self, uri, listenAdress, listenPort):
        try:
            self.ws = await websockets.connect(uri)
        except OSError as e:
            print(e.args)
            return
        print("websockets connection established !")

        asyncio.ensure_future(asyncio.start_server(self.new_tcp_income, listenAdress, listenPort))
        asyncio.ensure_future(self.send_to_peer())
        asyncio.ensure_future(self.read_from_peer())

    async def new_tcp_income(self, reader, writer):
        try:
            data = await reader.readexactly(3)
        except asyncio.IncompleteReadError as e:
            print(e.args)
            return
        if len(data) != 3:
            print("Error data len",file=sys.stderr)
            writer.close()
            return
        if data[0] != 0x05:
            print("Not socks5",file=sys.stderr)
            writer.close()
            return
        writer.write(b'\x05\x00')
        data = await reader.read(256)
        if data[0] != 0x05:
            print("Not socks5", file=sys.stderr)
            writer.close()
            return
        if data[1] != 0x01:
            print("Only support no authentication mnde", sys.stderr)
            writer.close()
            return
        if data[3] == 0x01:
            remote = socket.inet_ntop(socket.AF_INET, data[4:8])
            atyp = "IPV4"
        elif data[3] == 0x03:
            remote = (data[5:-2]).decode()
            atyp = "DOMAINNAME"
        elif data[3] == 0x04:
            remote = socket.inet_ntop(socket.AF_INET6, data[4:20])
            atyp = "IPV6"
        else:
            print("Unsupport protocol")
            writer.close()
            return

        port = int.from_bytes(data[-2:], byteorder='big', signed=False)

        fd = writer.get_extra_info('socket').fileno()
        stream = Stream(reader, writer, atyp, remote, port, fd, self)
        self.stream_map[fd] = stream
        asyncio.ensure_future(stream.run())


def main():
    loop = asyncio.get_event_loop()
    parser = argparse.ArgumentParser(description="Client part for crossing the GFW")
    parser.add_argument('-c', '--config', help="config file", default='config.json')
    parser.add_argument('-d', '--debug', help='enable debug output', default=False, action='store_true')
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level,
                        format='%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')

    with open(args.config) as f:
        config = json.load(f)

    if config['mode'] == "replace":
        cipher = Replace()
        cipher.load_key(config['key'])
    elif config['mode'] == 'aes-128-gcm':
        cipher = AES_128_GCM()
        cipher.load_key(config['key'])

    local = Local(cipher)
    asyncio.ensure_future(local.connect_to_peer("ws://%s:%d" % (config['serverAddress'], config["serverPort"]),
                                                config['localAddress'], config["localPort"]))
    loop.run_forever()


if __name__ == "__main__":
    main()
