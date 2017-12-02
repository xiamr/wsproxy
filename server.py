#!/usr/bin/env python3

import asyncio
import websockets
import msgpack
import json
import logging
import concurrent.futures
import argparse
from misc import *
import copy
import sys



class Stream:
    def __init__(self, stream_id, remote_addr_type: AddressType, remote_addr, remote_port, server ):
        self.stream_id = stream_id
        self.remote_addr_type = remote_addr_type
        self.remote_addr = remote_addr
        self.remote_port = remote_port
        self.server = server

        self.reader: asyncio.StreamReader = None
        self.writer: asyncio.StreamWriter = None

        self.write_queue = asyncio.Queue()
        self.write_queue_event = asyncio.Event()
        self.write_queue_event.set()

        self.read_task = None
        self.write_task = None

        self.write_and_close = False

    async def run(self):
        logging.info("connect to %s:%s ... : stream %s " % (addr_convert(self.remote_addr), self.remote_port, self.stream_id))
        try:
            self.reader, self.writer = await asyncio.open_connection(self.remote_addr, self.remote_port)
        except OSError as e:
            logging.info("connect to %s:%s failed %s: stream %s" % (addr_convert(self.remote_addr),
                                                                    self.remote_port, e.args, self.stream_id))
            await self.server.send_queue.put(encode_msg(msgtype=MsgType.Connection_Failure,
                                                        stream_id=self.stream_id),emergy=True)
            if self.stream_id in self.server.stream_map:
                del self.server.stream_map[self.stream_id]
            return

        logging.info("connect to %s:%s succeeded : stream %s" % (addr_convert(self.remote_addr), self.remote_port, self.stream_id))
        await self.server.send_queue.put(encode_msg(msgtype=MsgType.Connection_OK,
                                                    stream_id=self.stream_id),emergy=True)

        self.write_task = asyncio.ensure_future(self.write_to_server())
        self.read_task = asyncio.ensure_future(self.read_from_server())

    async def read_from_server(self):
        while True:
            try:
                raw_data = await self.reader.read(8196)
            except ConnectionError:
                logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                await self.server.send_queue.put(encode_msg(msgtype=MsgType.RClose,
                                                            stream_id=self.stream_id))
                return
            if len(raw_data) == 0:
                if self.reader.at_eof():
                    logging.debug("send rclose message to peer : stream %s" % self.stream_id)
                    await self.server.send_queue.put(encode_msg(msgtype=MsgType.RClose,
                                                                stream_id=self.stream_id))
                    return
            await self.server.send_queue.put(encode_msg(msgtype=MsgType.Data,
                                                        stream_id=self.stream_id,
                                                        data=raw_data))

    async def write_to_server(self):
        while True:

            if not self.write_and_close:
                await self.write_queue_event.wait()
            try:
                msg = self.write_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                self.write_queue_event.clear()
                if self.write_and_close:
                    logging.info("close writer : stream  %s" % self.stream_id)
                    if not self.writer.transport.is_closing():
                        self.writer.write_eof()
                    if self.stream_id in self.server.stream_map:
                        del self.server.stream_map[self.stream_id]

                    return
                continue

            logging.debug("send some data to server: stream %s" % self.stream_id)
            self.writer.write(msg.data)
            try:
                await self.writer.drain()
            except concurrent.futures.CancelledError:
                raise
            except ConnectionError:
                if not self.writer.transport.is_closing():
                    self.writer.write_eof()
                if self.stream_id in self.server.stream_map:
                    del self.server.stream_map[self.stream_id]

                return

    def rclose(self):
        self.write_and_close = True
        self.write_queue_event.set()

        if self.stream_id in self.server.stream_map:
            del self.server.stream_map[self.stream_id]


class UDPSession:
    def __init__(self, udpassociate, remote_addr, remote_port, remote_addr_type):
        self.udpassociate = udpassociate
        self.remote_addr = remote_addr
        self.remote_port = remote_port
        self.remote_addr_type = remote_addr_type
        self.transport = None


    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self,data, addr):
        logging.info("recv udp data from server %s:%s " %(self.remote_addr,self.remote_port))
        self.udpassociate.datagram_received(self.remote_addr_type, self.remote_addr,self.remote_port,data)


    def error_received(self, exc):
        pass

    def connection_lost(self, exc):
        pass

    def new_message_from_peer(self, msg):
        self.transport.sendto(msg.data)
        logging.info("Send one UDP data %s:%s" % (msg.remote_addr, msg.remote_port))


    def close(self):
        self.transport.close()
        self.to_close = True

class UDPAssociate:
    def __init__(self, client_addr, client_port, udprelay ):
        self.client_addr = client_addr
        self.client_port = client_port
        self.udprelay = udprelay

        self.udpsession_map: dict[(str,int), UDPSession] = {}

    async def new_message_from_peer(self, msg):
        if (msg.remote_addr, msg.remote_port) not in self.udpsession_map:
            trasport, protocol = await \
                asyncio.get_event_loop().create_datagram_endpoint(
                    lambda: UDPSession(self, msg.remote_addr, msg.remote_port, msg.remote_addr_type),
                    remote_addr=(msg.remote_addr,msg.remote_port))
            self.udpsession_map[(msg.remote_addr,msg.remote_port)] = protocol

        self.udpsession_map[(msg.remote_addr,msg.remote_port)].new_message_from_peer(msg)

    def datagram_received(self, remote_addr_type, remote_addr, remote_port, data):
        self.udprelay.send_array.append(encode_msg(msgtype=MsgType.UDP,
                                                   remote_addr_type=remote_addr_type,
                                                   remote_addr=remote_addr,
                                                   remote_port=remote_port,
                                                   client_addr=self.client_addr,
                                                   client_port=self.client_port,
                                                   data=data))
        self.udprelay.send_array_semaphore.release()

    async def close(self):

        for addr, session in self.udpsession_map.items():
            session.transport.close()


class UDPRelay:
    def __init__(self, server ):
        self.server = server

        self.udpassociate_map: dict[(str,int), UDPAssociate] = {}
        self.send_array = []
        self.send_array_semaphore = asyncio.Semaphore(0)

    async def new_message_from_peer(self,msg):
        if (msg.client_addr, msg.client_port) not in self.udpassociate_map:
            self.udpassociate_map[(msg.client_addr, msg.client_port)] = UDPAssociate(msg.client_addr,
                                                                                     msg.client_port, self)
        await self.udpassociate_map[(msg.client_addr, msg.client_port)].new_message_from_peer(msg)

    async def run(self):
        while True:
            await self.send_array_semaphore.acquire()
            await self.server.send_queue.put(self.send_array.pop(0))

    async def closeAssociate(self, msg):
        if (msg.client_addr, msg.client_port) in self.udpassociate_map:
            await self.udpassociate_map[(msg.client_addr, msg.client_port)].close()
            del self.udpassociate_map[(msg.client_addr, msg.client_port)]


class DNSRelay:
    def __init__(self, server):
        self.server = server
        self.send_array = []
        self.send_array_semaphore = asyncio.Semaphore(0)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self,data, addr):
        logging.info("recv dns replay ")
        self.send_array.append(data)
        self.send_array_semaphore.release()

    async def run(self):
        while True:
            await self.send_array_semaphore.acquire()
            await self.server.send_queue.put(encode_msg(msgtype=MsgType.DNSReplay,
                                                        data=self.send_array.pop(0)),emergy=True)

    def new_request_from_peer(self,msg):
        logging.info('new dns request')
        self.transport.sendto(msg.data)

    def error_received(self, exc):
        pass

    def connection_lost(self, exc):
        pass


class Server:
    def __init__(self, ws, cipher, normal_dns):
        self.ws = ws
        self.send_queue = TwoPrioQueue()
        self.stream_map = {}
        self.cipher = cipher
        self.close_tunnel = False
        self.udprelay = UDPRelay(self)
        self.udprelay_run_task = asyncio.ensure_future(self.udprelay.run())
        self.normal_dns = normal_dns

    async def run(self):
        send_task = asyncio.ensure_future(self.send_to_peer())

        trasport, self.dnsrelay = await \
            asyncio.get_event_loop().create_datagram_endpoint(
                lambda: DNSRelay(self), remote_addr=(self.normal_dns, 53))

        self.dnsrelay_run_task = asyncio.ensure_future(self.dnsrelay.run())
        await self.read_from_peer()
        if self.close_tunnel:
            send_task.cancel()

        self.udprelay_run_task.cancel()
        self.dnsrelay_run_task.cancel()

    async def send_to_peer(self):
        while True:
            data = await self.send_queue.get()
            try:
                await self.ws.send(self.cipher.encrypt(data))
            except websockets.exceptions.InvalidState as e:
                print("Websocket InvalidState : %s" % e.args)
                return

    async def read_from_peer(self):
        while True:
            try:
                data = await self.ws.recv()
            except websockets.exceptions.ConnectionClosed as e:
                logging.info("Websocket ConnectionClosed : %s" % e.args)
                return
            try:
                data = self.cipher.decrypt(data)
            except ValueError as e:
                logging.warning("Data is Invalid (Maybe GFW changed the packet): %s" % e.args)
                return

            msg = decode_msg(data)

            if msg.msgtype == MsgType.Connect:
                stream = Stream(msg.stream_id, msg.remote_addr_type, msg.remote_addr, msg.remote_port, self)
                self.stream_map[msg.stream_id] = stream
                logging.info("new stream: %s, remote_addr %s:%s" % (msg.stream_id, addr_convert(msg.remote_addr), msg.remote_port))
                asyncio.ensure_future(stream.run())
            elif msg.msgtype == MsgType.RClose:
                if msg.stream_id in self.stream_map:
                    self.stream_map[msg.stream_id].rclose()
            elif msg.msgtype == MsgType.UDP:
                await self.udprelay.new_message_from_peer(msg)
            elif msg.msgtype == MsgType.UDPClose:
                await self.udprelay.closeAssociate(msg)
            elif msg.msgtype == MsgType.CloseTunnel:
                self.close_tunnel = True
            elif msg.msgtype == MsgType.Data:
                logging.debug("new data from peer : stream %s " % msg.stream_id)
                if msg.stream_id in self.stream_map:
                    await self.stream_map[msg.stream_id].write_queue.put(msg)
                    self.stream_map[msg.stream_id].write_queue_event.set()
            elif msg.msgtype == MsgType.DNSRequest:
                self.dnsrelay.new_request_from_peer(msg)


cipher = None
normal_dns = None


async def new_ws_connection(ws, path):
    server = Server(ws, cipher,normal_dns)
    try:
        await server.run()
    except concurrent.futures.CancelledError as e:
        logging.info("Close Tunnel : %s" % e.args)




def main():
    global cipher, normal_dns

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

    normal_dns = config.pop('normal_dns','8.8.8.8')

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(websockets.serve(new_ws_connection, config["serverAddress"], config["serverPort"]))

    loop.run_forever()


if __name__ == "__main__":
    main()
