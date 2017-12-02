#!/usr/bin/env python3

import asyncio
import websockets
import ipaddress
import socket
import logging
import concurrent.futures
import argparse
from misc import *
import sys
from datetime import *



class Stream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 remote_addr_type: AddressType, remote_addr: str, remote_port: int, stream_id: int, local):

        self.reader = reader
        self.writer = writer
        self.remote_address_type = remote_addr_type
        self.remote_addr = remote_addr
        self.remote_port = remote_port
        self.stream_id = stream_id
        self.local = local

        self.read_task: asyncio.Task = None
        self.write_task: asyncio.Task = None

        self.write_queue = asyncio.Queue()
        self.write_queue_event = asyncio.Event()
        self.write_queue_event.set()

        self.write_and_close = False

    async def run(self):
        logging.info("start new stream : %s, %s:%s" % (self.stream_id, addr_convert(self.remote_addr), self.remote_port))
        await self.local.send_queue.put(encode_msg(msgtype=MsgType.Connect,
                                                   remote_addr_type=self.remote_address_type,
                                                   remote_addr=self.remote_addr,
                                                   remote_port=self.remote_port,
                                                   stream_id=self.stream_id), emergy=True)

    def connection_established(self):
        logging.info("new stream connected : %s, remote_addr: %s:%s" % (self.stream_id, addr_convert(self.remote_addr), self.remote_port))
        self.writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        self.read_task = asyncio.ensure_future(self.read_from_client())
        self.write_task = asyncio.ensure_future(self.write_to_client())

    def connection_failure(self):
        logging.info("new stream connect failed : %s, %s:%s" % (self.stream_id, addr_convert(self.remote_addr), self.remote_port))
        self.writer.write(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')
        self.writer.close()
        del self.local.stream_map[self.stream_id]

    async def read_from_client(self):
        while True:
            try:
                raw_data = await self.reader.read(8196)
            except ConnectionError:
                logging.info("send rclose message to peer : stream %s" % self.stream_id)
                await self.local.send_queue.put(encode_msg(msgtype=MsgType.RClose,
                                                           stream_id=self.stream_id))

                return
            if len(raw_data) == 0:
                if self.reader.at_eof():
                    logging.info("send rclose message to peer : stream %s" % self.stream_id)
                    await self.local.send_queue.put(encode_msg(msgtype=MsgType.RClose,
                                                               stream_id=self.stream_id))
                    return

            await self.local.send_queue.put(encode_msg(msgtype=MsgType.Data,
                                                       stream_id=self.stream_id,
                                                       data=raw_data))

    async def write_to_client(self):
        while True:
            if not self.write_and_close:
                await self.write_queue_event.wait()
            try:
                msg = self.write_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                self.write_queue_event.clear()
                if self.write_and_close:
                    logging.info("close writer : stream %s" % self.stream_id)
                    if not self.writer.transport.is_closing():
                        self.writer.write_eof()
                    if self.stream_id in self.local.stream_map:
                        del self.local.stream_map[self.stream_id]

                    return
                continue

            logging.debug("send some data to client: stream %s" % self.stream_id)
            self.writer.write(msg.data)
            try:
                await self.writer.drain()
            except concurrent.futures.CancelledError:
                raise
            except ConnectionError:
                if not self.writer.transport.is_closing():
                    self.writer.write_eof()
                if self.stream_id in self.local.stream_map:
                    del self.local.stream_map[self.stream_id]

                return

    def rclose(self):
        logging.debug("prepare to close writer : stream %s" % self.stream_id)
        self.write_and_close = True
        self.write_queue_event.set()
        if self.stream_id in self.local.stream_map:
            del self.local.stream_map[self.stream_id]


class UDPAssociate:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 client_addr_type: AddressType, client_addr: str, client_port: int):
        self.reader = reader
        self.writer = writer
        self.client_addr_type = client_addr_type
        self.client_addr = client_addr
        self.client_port = client_port

        self.data_array_semaphore = asyncio.Semaphore(0)
        self.data_array = []

        self.run_task = asyncio.ensure_future(self.run())
        asyncio.ensure_future(self.read())

        self.relay: UDPRelay = None

    def add_relay(self, relay):
        self.relay = relay

    async def read(self):
        try:
            data = await self.reader.read()
        except ConnectionError:
            self.run_task.cancel()
            if (self.client_addr, self.client_port) in self.relay.udpassociate_map:
                await self.relay.send_queue.put(encode_msg(msgtype=MsgType.UDPClose,
                                                           client_addr=self.client_addr,
                                                           client_port=self.client_port))
                logging.info("close udp associate : %s:%s" % (addr_convert(self.client_addr), self.client_port))
                del self.relay.udpassociate_map[(self.client_addr, self.client_port)]
            return

        if len(data) == 0:
            self.run_task.cancel()
            if (self.client_addr, self.client_port) in self.relay.udpassociate_map:
                await self.relay.send_queue.put(encode_msg(msgtype=MsgType.UDPClose,
                                                           client_addr=self.client_addr,
                                                           client_port=self.client_port))
                logging.info("close udp associate : %s:%s" % (addr_convert(self.client_addr), self.client_port))
                del self.relay.udpassociate_map[(self.client_addr, self.client_port)]
            return
        logging.warning("some problem happened:  data = %s, client = %s:%s" % (data, addr_convert(self.client_addr), self.client_port))

    async def run(self):
        while True:
            await self.data_array_semaphore.acquire()
            data = self.data_array.pop(0)
            if data[3] == 0x01:
                remote_addr_type = AddressType.IPv4
                remote_addr = socket.inet_ntop(socket.AF_INET, data[4:8])
                remote_port = int.from_bytes(data[8:10], byteorder='big', signed=False)
            elif data[3] == 0x03:
                remote_addr_type = AddressType.DomainName
                remote_addr = data[5:5+data[4]].decode()
                remote_port = int.from_bytes(data[5+data[4]:7+data[4]], byteorder='big', signed=False)
            elif data[3] == 0x04:
                remote_addr_type = AddressType.IPv6
                remote_addr = socket.inet_ntop(socket.AF_INET6, data[4:20])
                remote_port = int.from_bytes(data[20:22], byteorder='big', signed=False)

            logging.info("send udp data to remote_addr %s:%s, %s:%s" %
                         (remote_addr, remote_port, addr_convert(self.client_addr), self.client_port))

            await self.relay.send_queue.put(encode_msg(msgtype=MsgType.UDP,
                                                       client_addr=self.client_addr,
                                                       client_port=self.client_port,
                                                       remote_addr_type=remote_addr_type,
                                                       remote_addr=remote_addr,
                                                       remote_port=remote_port,
                                                       data=data[10:]))

    def new_data_from_client(self,data):
        if data[2] != 0x00:  # FRAG always is 0x00
            return
        logging.info("append some udp data")
        self.data_array.append(data)
        self.data_array_semaphore.release()

    def send_data_to_client(self, data, remote_addr, remote_port, remote_addr_type):
        logging.info("send udp data to client %s:%s  %s:%s" % (self.client_addr, self.client_port,
                                                               remote_addr, remote_port))
        if remote_addr_type == AddressType.IPv4:
            self.relay.transport.sendto(b'\x00\x00\x00\x01' +
                                    socket.inet_pton(socket.AF_INET,remote_addr) +
                                    remote_port.to_bytes(2, byteorder='big', signed=False) +
                                    data, (self.client_addr, self.client_port))
        elif remote_addr_type == AddressType.IPv6:
            self.relay.transport.sendto(b'\x00\x00\x00\x04' +
                                    socket.inet_pton(socket.AF_INET6,remote_addr) +
                                    remote_port.to_bytes(2, byteorder='big', signed=False) +
                                    data, (self.client_addr, self.client_port))
        elif remote_addr_type == AddressType.DomainName:
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
        remote_addr = addr[0]
        remote_port = addr[1]
        logging.info("datagram_received from %s:%s" % (addr_convert(remote_addr), remote_port))
        if (remote_addr,remote_port) in self.udpassociate_map:
            self.udpassociate_map[(remote_addr,remote_port)].new_data_from_client(data)
        else:
            for client_addr , client_port in self.udpassociate_map:
                if client_port == remote_port and (client_addr == '0.0.0.0' or client_addr == '::'):
                    udp = self.udpassociate_map[(client_addr, remote_port)]
                    udp.client_addr = remote_addr
                    self.udpassociate_map[(remote_addr,remote_port)] = udp
                    udp.new_data_from_client(data)
                    return
            for client_addr, client_port in self.udpassociate_map:
                if client_port == 0 and (client_addr == '0.0.0.0' or client_addr == '::'):
                    udp = self.udpassociate_map[(client_addr,0)]
                    udp.client_addr = remote_addr
                    udp.client_port = remote_port
                    self.udpassociate_map[(remote_addr,remote_port)] = udp
                    udp.new_data_from_client(data)
                    return

    def error_received(self, exc):
        pass

    def addUDPAssociate(self, udpassocaite : UDPAssociate):
        if self.listenAddr == '0.0.0.0' or self.listenAddr == '::':
            sockname =  udpassocaite.writer.get_extra_info('sockname')
            bindAddress = sockname[0]
        else:
            bindAddress = self.listenAddr
        try:
            ipaddr = ipaddress.ip_address(bindAddress)
            if isinstance(ipaddr,ipaddress.IPv4Address):
                addressbyte = b'\x01' + socket.inet_pton(socket.AF_INET,bindAddress)
            elif isinstance(ipaddr,ipaddress.IPv6Address):
                addressbyte = b'\x04' + socket.inet_pton(socket.AF_INET6,bindAddress)
        except ValueError:
            raise SystemExit(1)
        udpassocaite.writer.write(b'\x05\x00\x00' + addressbyte +
                                 self.listenPort.to_bytes(2, byteorder='big', signed=False))
        udpassocaite.add_relay(self)

        self.udpassociate_map[(udpassocaite.client_addr, udpassocaite.client_port)] = udpassocaite
        logging.info("create new udp associate %s:%s" % (udpassocaite.client_addr, udpassocaite.client_port))

    def new_data_from_peer(self, msg):
        if (msg.client_addr, msg.client_port) in self.udpassociate_map:
            self.udpassociate_map[(msg.client_addr,msg.client_port)].send_data_to_client(
                msg.data ,msg.remote_addr,msg.remote_port, msg.remote_addr_type)


class DNSRelay:
    def __init__(self, local):
        self.transaction_map: dict[bytes, (str, int)] = {}
        self.local = local
        self.data_array_semaphore = asyncio.Semaphore(0)
        self.data_array = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self,data, addr):  #(ip:client_port)
        remote_addr = addr[0]
        remote_port = addr[1]
        logging.info("DNS request from %s:%s" % (addr_convert(remote_addr), remote_port))
        self.transaction_map[data[:2]] = (remote_addr, remote_port)
        asyncio.ensure_future(self.delete_transaction(data[:2]))
        self.data_array.append(data)
        self.data_array_semaphore.release()

    async def delete_transaction(self, tid):
        await asyncio.sleep(60)
        try:
            del self.transaction_map[tid]
        except KeyError:
            pass

    async def run(self):
        while True:
            await self.data_array_semaphore.acquire()
            logging.info('send DNS request')
            await self.local.send_queue.put(encode_msg(msgtype=MsgType.DNSRequest,
                                             data=self.data_array.pop(0)), emergy=True)

    def replay_from_peer(self, msg):
        try:
            self.transport.sendto(msg.data, self.transaction_map.pop(msg.data[:2]))
        except KeyError:
            pass

    def error_received(self, exc):
        pass


class Local:
    def __init__(self, cipher):
        self.ws = None
        self.stream_map = {}
        self.send_queue = TwoPrioQueue()
        self.cipher = cipher
        self.last_active_time = datetime.now()
        self.close_tunnel = False

        self.udprelay: UDPRelay = None

        self.dnsrelay: DNSRelay = None

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
            msg = decode_msg(data)
            if msg.msgtype == MsgType.RClose:
                if msg.stream_id in self.stream_map:
                    self.stream_map[msg.stream_id].rclose()
            elif msg.msgtype == MsgType.Connection_OK:
                if msg.stream_id in self.stream_map:
                    self.stream_map[msg.stream_id].connection_established()
            elif msg.msgtype == MsgType.Connection_Failure:
                if msg.stream_id in self.stream_map:
                    self.stream_map[msg.stream_id].connection_failure()
            elif msg.msgtype == MsgType.UDP:
                self.udprelay.new_data_from_peer(msg)
            elif msg.msgtype == MsgType.Data:
                if msg.stream_id in self.stream_map:
                    await self.stream_map[msg.stream_id].write_queue.put(msg)
                    self.stream_map[msg.stream_id].write_queue_event.set()
            elif msg.msgtype == MsgType.DNSReplay:
                self.dnsrelay.replay_from_peer(msg)

    async def connect_to_peer(self, serverAddress, serverPort,
                              listenAddress, listenPort, dnsrelay: bool, reconnect=False):

        self.uri = "ws://%s:%s" % (addr_convert(serverAddress), serverPort)
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
            asyncio.ensure_future(asyncio.start_server(self.new_tcp_income, "0.0.0.0", listenPort))
            transport, self.udprelay = await \
                asyncio.get_event_loop().create_datagram_endpoint(
                    lambda: UDPRelay(self,self.listenAddres,self.listenPort),
                       local_addr=(listenAddress, listenPort))
            if dnsrelay:
                transport, self.dnsrelay = await  \
                    asyncio.get_event_loop().create_datagram_endpoint(
                        lambda: DNSRelay(self),
                        local_addr=(listenAddress,53))

                asyncio.ensure_future(self.dnsrelay.run())

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
                await self.send_queue.put(encode_msg(msgtype=MsgType.CloseTunnel))
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
        try:
            data = await reader.readexactly(4)
        except asyncio.IncompleteReadError as e:
            print(e.args)
            writer.close()
            return

        if data[0] != 0x05:
            print("Not socks5", file=sys.stderr)
            writer.close()
            return
        if data[1] == 0x03:
            # UDP Associate
            if data[3] == 0x01:
                try:
                    data = await reader.readexactly(4)
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return

                client_addr = socket.inet_ntop(socket.AF_INET, data)
                remote_addr_type = AddressType.IPv4
            elif data[3] == 0x04:
                try:
                    data = await reader.readexactly(16)
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return

                client_addr= socket.inet_ntop(socket.AF_INET6, data)
                remote_addr_type = AddressType.IPv6
            else:
                print("Unsupported address msgtype", file=sys.stderr)
                writer.close()
                return
            try:
                data =  await reader.readexactly(2)
            except asyncio.IncompleteReadError as e:
                print(e.args)
                writer.close()
                return

            remote_port = int.from_bytes(data, byteorder='big', signed=False)
            udpassociate = UDPAssociate(reader,writer,remote_addr_type,client_addr,remote_port)
            self.udprelay.addUDPAssociate(udpassociate)

        else:
            if data[3] == 0x01:
                try:
                    data = await reader.readexactly(4)
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return

                remoter_addr = socket.inet_ntop(socket.AF_INET, data)
                remote_addr_type = AddressType.IPv4
            elif data[3] == 0x03:
                try:
                    data = await reader.readexactly(1)
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return
                try:
                    data = await reader.readexactly(int.from_bytes(data,byteorder='big',signed=False))
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return

                remoter_addr = data.decode()
                remote_addr_type = AddressType.DomainName
            elif data[3] == 0x04:
                try:
                    data = await reader.readexactly(16)
                except asyncio.IncompleteReadError as e:
                    print(e.args)
                    writer.close()
                    return

                remoter_addr = socket.inet_ntop(socket.AF_INET6, data)
                remote_addr_type = AddressType.IPv6
            else:
                print("Unsupported address msgtype")
                writer.close()
                return
            try:
                data = await reader.readexactly(2)
            except asyncio.IncompleteReadError as e:
                print(e.args)
                writer.close()
                return
            remote_port = int.from_bytes(data, byteorder='big', signed=False)

            fd = writer.get_extra_info('socket').fileno()
            stream = Stream(reader, writer, remote_addr_type, remoter_addr, remote_port, fd, self)
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
                                                config['localAddress'], config["localPort"],
                                                config.pop('dnsrelay',False)))
    loop.run_forever()


if __name__ == "__main__":
    main()
