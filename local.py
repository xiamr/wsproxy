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
from datetime import *
import os

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
                    if self.stream_id in self.local.stream_map:
                        del self.local.stream_map[self.stream_id]

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
                if self.stream_id in self.local.stream_map:
                    del self.local.stream_map[self.stream_id]

                return

    def rclose(self):
        logging.debug("prepare to close writer : stream %s" % self.stream_id)
        self.write_and_close = True
        self.new_write_queue.set()
        if self.stream_id in self.local.stream_map:
            del self.local.stream_map[self.stream_id]


class UDPAssociate:
    def __init__(self, reader, writer, atyp, client_addr, client_port):
        self.reader = reader
        self.writer = writer
        self.atyp = atyp
        self.client_addr = client_addr
        self.client_port = client_port
        self.new_data_come_semaphore = asyncio.Semaphore(0)
        self.data_array = []
        self.run_task = asyncio.ensure_future(self.run())
        asyncio.ensure_future(self.read())

    def addRelay(self, relay):
        self.relay = relay

    async def read(self):
        try:
            data =  await self.reader.read()
        except ConnectionError:
            self.run_task.cancel()
            if (self.client_addr, self.client_port) in self.relay.udpassociate_map:
                await self.relay.send_queue.put(pack({'METHOD':'UDP','TYPE':'CLOSE',
                                                      'CLIENT': (self.client_addr, self.client_port)}))
                logging.info("close udp associate")
                del self.relay.udpassociate_map[(self.client_addr, self.client_port)]
                return

        if len(data) == 0:
            self.run_task.cancel()
            if (self.client_addr, self.client_port) in self.relay.udpassociate_map:
                await self.relay.send_queue.put(pack({'METHOD': 'UDP', 'TYPE': 'CLOSE',
                                                      'CLIENT': (self.client_addr, self.client_port)}))
                logging.info("close udp associate")
                del self.relay.udpassociate_map[(self.client_addr, self.client_port)]
                return
        logging.info("some problem happend")

    async def run(self):
        while True:
            await self.new_data_come_semaphore.acquire()
            data = self.data_array.pop(0)
            if data[3] == 0x01:
                atyp = 'IPV4'
                remote_addr = socket.inet_ntop(socket.AF_INET, data[4:8])
                remote_port = int.from_bytes(data[8:10], byteorder='big', signed=False)
            elif data[3] == 0x03:
                atyp = 'DOMAINNAME'
                remote_addr = data[5:5+data[4]].decode()
                remote_port = int.from_bytes(data[5+data[4]:7+data[4]], byteorder='big', signed=False)
            elif data[3] == 0x04:
                atyp = 'IPV6'
                remote_addr = socket.inet_ntop(socket.AF_INET, data[4:20])
                remote_port = int.from_bytes(data[20:22], byteorder='big', signed=False)

            logging.info("send udp data to remote %s:%s" % (remote_addr, remote_port))
            await self.relay.send_queue.put(pack({'METHOD': 'UDP',
                                                  'TYPE': 'DATA',
                                                  'CLIENT': (self.client_addr, self.client_port),
                                                  'ATYP' : atyp,
                                                  'REMOTE': (remote_addr, remote_port),
                                                  'DATA': data[10:]}))

    def new_data_from_client(self,data):
        if data[2] != 0x00:  # FRAG always is 0x00
            return
        logging.info("append some udp data")
        self.data_array.append(data)
        self.new_data_come_semaphore.release()

    def send_data_to_client(self, data, remote_addr, remote_port, atyp):
        logging.info("send udp data to client %s:%s  %s:%s" %(self.client_addr,self.client_port, remote_addr,remote_port))
        if atyp == 'IPV4':
            self.relay.transport.sendto(b'\x00\x00\x00\x01' +
                                    socket.inet_pton(socket.AF_INET,remote_addr) +
                                    remote_port.to_bytes(2, byteorder='big', signed=False) +
                                    data, (self.client_addr, self.client_port))
        elif atyp == 'IPV6':
            self.relay.transport.sendto(b'\x00\x00\x00\x04' +
                                    socket.inet_pton(socket.AF_INET6,remote_addr) +
                                    remote_port.to_bytes(2, byteorder='big', signed=False) +
                                    data, (self.client_addr, self.client_port))
        elif atyp == 'DOMAINNAME':
            self.relay.transport.sendto(b'\x00\x00\x00\x03' +
                                        len(remote_addr).to_bytes(1,byteorder='big', signed=False) +
                                        remote_addr.encode() +
                                        remote_port.to_bytes(2, byteorder='big', signed=False) +
                                        data, (self.client_addr, self.client_port))




class UDPRelay:
    def __init__(self, local , listenAddress, listenPort):
        self.udpassociate_map : dict[(str,int),UDPAssociate] = {}
        self.listenAddr = listenAddress
        self.listenPort = listenPort
        self.local = local
        self.send_queue = self.local.send_queue

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self,data, addr):  #(ip:client_port)
        if addr in self.udpassociate_map:
            self.udpassociate_map[addr].new_data_from_client(data)

    def error_received(self, exc):
        pass

    def addUDPAssociate(self, udpassocaite : UDPAssociate):
        # Only for IPv4
        try:
            ipaddr = ipaddress.ip_address(self.listenAddr)
            if isinstance(ipaddr,ipaddress.IPv4Address):
                addressbyte = b'\x01' + socket.inet_pton(socket.AF_INET,self.listenAddr)
            elif isinstance(ipaddr,ipaddress.IPv6Address):
                addressbyte = b'\x04' + socket.inet_pton(socket.AF_INET6,self.listenAddr)
        except ValueError:
            addressbyte = b'\x03' + len(self.listenAddr).to_bytes(1,byteorder='big',signed=False)
        udpassocaite.writer.write(b'\x05\x00\x00\x01' + addressbyte +
                                 self.listenPort.to_bytes(2, byteorder='big', signed=False))
        udpassocaite.addRelay(self)
        if udpassocaite.client_addr == '0.0.0.0':
            udpassocaite.client_addr = '127.0.0.1'
        self.udpassociate_map[(udpassocaite.client_addr, udpassocaite.client_port)] = udpassocaite
        logging.info("create new udp associate %s:%s" % (udpassocaite.client_addr, udpassocaite.client_port))

    def new_data_from_peer(self, message):
        if tuple(message['CLIENT']) in self.udpassociate_map:
            remote_addr, remote_port = tuple(message['REMOTE'])
            self.udpassociate_map[tuple(message['CLIENT'])].send_data_to_client(
                message['DATA'],remote_addr,remote_port, message['ATYP'])



class Local:
    def __init__(self, cipher):
        self.ws = None
        self.stream_map = {}
        self.send_queue = asyncio.Queue()
        self.cipher = cipher
        self.last_active_time = datetime.now()
        self.close_tunnel = False

        self.udprelay = None

    async def send_to_peer(self):
        while True:
            data = await self.send_queue.get()
            try:
                await self.ws.send(self.cipher.encrypt(data))
            except websockets.exceptions.InvalidState as e:
                print("Websocket InvalidState : %s" % e.args)
                return
            self.last_active_time = datetime.now()

    async def read_from_peer(self):
        while True:
            try:
                data = await self.ws.recv()
            except websockets.exceptions.ConnectionClosed as e:
                logging.info("Websocket ConnectionClosed : %s" % e.args)
                return
            self.last_active_time = datetime.now()
            try:
                data = self.cipher.decrypt(data)
            except ValueError as e:
                logging.warning("Data is Invalid (Maybe GFW changed the packet): %s" % e.args)
                return
            message = unpack(data)
            if 'ID' in message:
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
                elif message['METHOD'] == "UDP":
                    if message['TYPE'] == 'DATA':
                        self.udprelay.new_data_from_peer(message)
            else:
                logging.debug("recv data from peer : stream %s" % ID)
                if ID in self.stream_map:
                    await self.stream_map[ID].write_queue.put(message)
                    self.stream_map[ID].new_write_queue.set()

    async def connect_to_peer(self, serverAddress, serverPort, listenAddress, listenPort, reconnect = False):
        self.uri = "ws://%s:%d" % (serverAddress, serverPort)
        self.listenAddres = listenAddress
        self.listenPort = listenPort
        try:
            self.ws = await websockets.connect(self.uri)
        except OSError as e:
            print(e.args)
            raise SystemExit(1)
            return
        print("websockets connection established !")

        if not reconnect:
            asyncio.ensure_future(asyncio.start_server(self.new_tcp_income, listenAddress, listenPort))
            transport, self.udprelay = await \
                asyncio.get_event_loop().create_datagram_endpoint(
                    lambda: UDPRelay(self,self.listenAddres,self.listenPort),
                       local_addr=(listenAddress, listenPort))
        asyncio.ensure_future(self.send_to_peer())
        asyncio.ensure_future(self.read_from_peer())
        asyncio.ensure_future(self.check_connection())


    async def reconnect(self):
        await self.connect_to_peer(self.uri, self.listenAddres, self.listenPort, True)

    async def check_connection(self):
        while True:
            await asyncio.sleep(60)
            delta = datetime.now() - self.last_active_time
            if delta.total_seconds() > 3600:
                self.close_tunnel = True
                await self.send_queue.put(pack({'METHOD':"CLOSETUNNEL"}))
                return

    async def new_tcp_income(self, reader, writer):
        if self.close_tunnel:
            await self.reconnect()
            self.close_tunnel = False

        self.last_active_time = datetime.now()
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
        if data[1] == 0x03:
            # UDP Associate
            if data[3] == 0x01:
                client_addr = socket.inet_ntop(socket.AF_INET, data[4:8])
                atyp = "IPV4"
            elif data[3] == 0x04:
                client_addr= socket.inet_ntop(socket.AF_INET6, data[4:20])
                atyp = "IPV6"
            else:
                print("Unsupported address type", file=sys.stderr)
                writer.close()
                return

            port = int.from_bytes(data[-2:], byteorder='big', signed=False)
            udpassociate = UDPAssociate(reader,writer,atyp,client_addr,port)
            self.udprelay.addUDPAssociate(udpassociate)

        else:
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
                print("Unsupported address type")
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
    else:
        print("Unsupported mode",file=sys.stderr)

    local = Local(cipher)
    asyncio.ensure_future(local.connect_to_peer(config['serverAddress'], config["serverPort"],
                                                config['localAddress'], config["localPort"]))
    loop.run_forever()


if __name__ == "__main__":
    main()
