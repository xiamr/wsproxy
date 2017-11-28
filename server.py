#!/usr/bin/env python3

import asyncio
import websockets
import msgpack
import json
import logging
import concurrent.futures
import argparse
from cipher_algorithm import *
import copy
import sys


class Stream:
    def __init__(self, stream_id, atyp, remote, port, server):
        self.stream_id = stream_id
        self.atyp = atyp
        self.remote = remote
        self.port = port
        self.server = server

        self.reader = None
        self.writer = None

        self.write_queue = asyncio.Queue()
        self.new_write_queue = asyncio.Event()
        self.new_write_queue.set()

        self.read_task = None
        self.write_task = None

        self.write_and_close = False

    async def run(self):
        logging.info("connect to %s:%s ... : stream %s " % (self.remote, self.port, self.stream_id))
        try:
            self.reader, self.writer = await asyncio.open_connection(self.remote, self.port)
        except OSError as e:
            logging.info("connect to %s:%s failed %s: stream %s" % (self.remote, self.port, e.args, self.stream_id))
            await self.server.send_queue.put(pack({'METHOD': 'CONNECTIONFAILURE', 'ID': self.stream_id}))
            del self.server.stream_map[self.stream_id]
            return

        logging.info("connect to %s:%s succeeded : stream %s" % (self.port, self.remote, self.stream_id))
        await self.server.send_queue.put(pack({'METHOD': "CONNECTIONOK", 'ID': self.stream_id}))

        self.write_task = asyncio.ensure_future(self.write_to_server())
        self.read_task = asyncio.ensure_future(self.read_from_server())

    async def read_from_server(self):
        while True:
            try:
                raw_data = await self.reader.read(8196)
            except ConnectionResetError:
                logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                await self.server.send_queue.put(pack({"METHOD": "RCLOSE", "ID": self.stream_id}))
                return
            if len(raw_data) == 0:
                if self.reader.at_eof():
                    logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                    await self.server.send_queue.put(pack({"METHOD": "RCLOSE", "ID": self.stream_id}))
                    return
            logging.debug("recv some data from server : Stream %s" % self.stream_id)
            await self.server.send_queue.put(pack({"ID": self.stream_id, "DATA": raw_data}))

    async def write_to_server(self):
        while True:

            if not self.write_and_close:
                await self.new_write_queue.wait()
            try:
                message = self.write_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                self.new_write_queue.clear()
                if self.write_and_close:
                    logging.info("close writer : stream  %s" % self.stream_id)
                    if not self.writer.transport.is_closing():
                        self.writer.write_eof()
                    return
                continue

            logging.debug("send some data to server: stream %s" % self.stream_id)
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
        self.write_and_close = True
        self.new_write_queue.set()

        del self.server.stream_map[self.stream_id]


class Server:
    def __init__(self, ws, cipher):
        self.ws = ws
        self.send_queue = asyncio.Queue()
        self.stream_map = {}
        self.cipher = cipher

    async def run(self):
        asyncio.ensure_future(self.send_to_peer())
        await self.read_from_peer()

    async def send_to_peer(self):
        while True:
            data = await self.send_queue.get()
            try:
                await self.ws.send(self.cipher.encrypt(data))
            except websockets.exceptions.InvalidState as e:
                print("Websocket InvalidState  %s" % e.args)
                return

    async def read_from_peer(self):
        while True:
            try:
                data = await self.ws.recv()
                data = self.cipher.decrypt(data)
            except websockets.exceptions.ConnectionClosed as e:
                logging.info("Websocket ConnectionClosed %s" % e.args)
                return
            message = unpack(data)
            ID = message['ID']
            if "METHOD" in message:
                if message["METHOD"] == "CONNECT":
                    stream = Stream(ID, message["ATYP"], message["REMOTE"], message["PORT"], self)
                    self.stream_map[stream.stream_id] = stream
                    logging.info("new stream: %s, remote %s:%s" % (ID, message["REMOTE"], message["PORT"]))
                    asyncio.ensure_future(stream.run())

                elif message['METHOD'] == 'RCLOSE':
                    if ID in self.stream_map:
                        self.stream_map[ID].rclose()
            else:
                logging.debug("new data from peer : Stream %s " % ID)
                if ID in self.stream_map:
                    await self.stream_map[ID].write_queue.put(message)
                    self.stream_map[ID].new_write_queue.set()


cipher = None


async def new_ws_connection(ws, path):
    server = Server(ws, copy.deepcopy(cipher))
    await server.run()


def main():
    global cipher

    parser = argparse.ArgumentParser(description="Server part for crossing the GFW")
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
    else:
        print("Unsupported mode",file=sys.stderr)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(websockets.serve(new_ws_connection, "0.0.0.0", config["serverPort"]))
    asyncio.ensure_future(websockets.serve(new_ws_connection, "::", config["serverPort"]))

    loop.run_forever()


if __name__ == "__main__":
    main()
