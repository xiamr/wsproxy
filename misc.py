import msgpack
import bz2
import json
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import base64
from Crypto.Hash import MD5
import random
import enum
import logging
import traceback
import pickle
import ipaddress
import asyncio
import random

padding_content = bytearray(1024)

enable_compress: bool = True


class TwoPrioQueue:
    def __init__(self,maxsize = 0):
        self.emergy_queue = []
        self.ordinary_queue = []
        self.put_event = asyncio.Event()
        self.get_event = asyncio.Event()
        self.maxsize = maxsize

    async def put(self, item, emergy=False):
        if self.full():
            self.get_event.clear()
            await self.get_event.wait()
        self.get_event.clear()
        if emergy:
            self.emergy_queue.append(item)
        else:
            self.ordinary_queue.append(item)
        self.put_event.set()

    async def get(self):
        if self.empty():
            self.put_event.clear()
            await self.put_event.wait()
        self.put_event.clear()
        self.get_event.set()
        if len(self.emergy_queue) > 0:
            return self.emergy_queue.pop(0)
        return self.ordinary_queue.pop(0)

    def qsize(self):
        return len(self.emergy_queue) + len(self.ordinary_queue)

    def empty(self):
        return self.qsize() == 0

    def full(self):
        if self.maxsize > 0:
            return self.qsize() == self.maxsize
        return False

    def get_nowait(self):
        if self.empty():
            raise asyncio.QueueEmpty()
        else:
            self.get_event.set()
            if len(self.emergy_queue) > 0:
                return self.emergy_queue.pop(0)
            return self.ordinary_queue.pop(0)

    def put_nowait(self, item, emergy=False):
        if self.full():
            raise asyncio.QueueFull()
        else:
            if emergy:
                self.emergy_queue.append(item)
            else:
                self.ordinary_queue.append(item)
        self.put_event.set()


def addr_convert(address):
    try:
        ipaddr = ipaddress.ip_address(address)
        if isinstance(ipaddr, ipaddress.IPv6Address):
            return '[%s]' % address
    except:
        pass
    return address


PUBLIC_ENUMS = {}


def msgpack_enum_register(cls):
    PUBLIC_ENUMS[cls.__name__] = cls
    return cls


def encode_enum(obj):
    if type(obj) in PUBLIC_ENUMS.values():
        return {"__enum__": str(obj)}


def decode_enum(obj):
    if '__enum__' in obj:
        name, member = obj['__enum__'].split(".")
        return getattr(PUBLIC_ENUMS[name],member)
    else:
        return obj


def pack(plaintext):
    data = msgpack.packb(plaintext, use_bin_type=True, default=encode_enum)
    return bz2.compress(data) if enable_compress else data


def unpack(data):
    data = bz2.decompress(data) if enable_compress else data
    return msgpack.unpackb(data, encoding='utf-8', object_hook=decode_enum)


@msgpack_enum_register
@enum.unique
class AddressType(enum.Enum):
    IPv4 = 'IPV4'
    IPv6 = 'IPV6'
    DomainName = 'DOMAINNAME'


@msgpack_enum_register
@enum.unique
class MsgType(enum.Enum):
    Connect = 'CONNECT'
    Connection_OK = 'CONNECTIONOK'
    Connection_Failure = 'CONNECTIONFAILURE'
    RClose = 'RCLOSE'
    UDP = 'UDP'
    UDPClose = 'UDPCLOSE'
    CloseTunnel = 'CLOSETUNNEL'
    Data = 'DATA'
    DNSRequest = 'DNSREQUEST'
    DNSReplay = 'DNSREPLAY'


class Msg:
    pass


def decode_msg(data):
    message = unpack(data)
    msg = Msg()
    msg.msgtype = message['MSGTYPE']
    if msg.msgtype == MsgType.Connect:
        msg.remote_addr_type = message['REMOTEADDRTYPE']
        msg.remote_addr = message['REMOTEADDR']
        msg.remote_port = message['REMOTEPORT']
        msg.stream_id = message['STREAMID']
    elif msg.msgtype == MsgType.RClose:
        msg.stream_id= message['STREAMID']
    elif msg.msgtype == MsgType.Connection_OK:
        msg.stream_id = message['STREAMID']
    elif msg.msgtype == MsgType.Connection_Failure:
        msg.stream_id = message['STREAMID']
    elif msg.msgtype == MsgType.UDP:
        msg.data = message['DATA']
        msg.client_addr = message['CLIENTADDR']
        msg.client_port = message['CLIENTPORT']
        msg.remote_addr = message['REMOTEADDR']
        msg.remote_port = message['REMOTEPORT']
        msg.remote_addr_type = message['REMOTEADDRTYPE']
    elif msg.msgtype == MsgType.UDPClose:
        msg.client_addr = message['CLIENTADDR']
        msg.client_port = message['CLIENTPORT']
    elif msg.msgtype == MsgType.Data:
        msg.stream_id = message['STREAMID']
        msg.data = message['DATA']
    elif msg.msgtype == MsgType.CloseTunnel:
        pass
    elif msg.msgtype == MsgType.DNSRequest:
        msg.data = message['DATA']
    elif msg.msgtype == MsgType.DNSReplay:
        msg.data = message['DATA']
    return msg


def encode_msg(**kwargs):

    msgtype = kwargs.pop('msgtype')
    message = {'MSGTYPE' : msgtype}
    if msgtype == MsgType.Connect:
        message['REMOTEADDRTYPE'] = kwargs['remote_addr_type']
        message['REMOTEADDR'] = kwargs['remote_addr']
        message['REMOTEPORT'] = kwargs['remote_port']
        message['STREAMID'] = kwargs['stream_id']
        message['PADDING'] = padding_content[:random.randint(0, 1024)]
    elif msgtype == MsgType.RClose:
        message['STREAMID'] = kwargs['stream_id']
    elif msgtype == MsgType.Connection_OK:
        message['STREAMID'] = kwargs['stream_id']
    elif msgtype == MsgType.Connection_Failure:
        message['STREAMID'] = kwargs['stream_id']
    elif msgtype == MsgType.UDP:
        message['CLIENTADDR'] = kwargs['client_addr']
        message['CLIENTPORT'] = kwargs['client_port']
        message['REMOTEADDR'] = kwargs['remote_addr']
        message['REMOTEPORT'] = kwargs['remote_port']
        message['REMOTEADDRTYPE'] = kwargs['remote_addr_type']
        message['DATA'] = kwargs['data']
    elif msgtype == MsgType.UDPClose:
        message['CLIENTADDR'] = kwargs['client_addr']
        message['CLIENTPORT'] = kwargs['client_port']
    elif msgtype == MsgType.Data:
        message['STREAMID'] = kwargs['stream_id']
        message['DATA'] = kwargs['data']
    elif msgtype == MsgType.CloseTunnel:
        pass
    elif msgtype == MsgType.DNSRequest:
        message['DATA'] = kwargs['data']
    elif msgtype == MsgType.DNSReplay:
        message['DATA'] = kwargs['data']

    return pack(message)


class NoEncrypt:
    def encrypt(self,data):
        return data

    def decrypt(self,data):
        return data

    def load_key(self,passphrase):
        pass

class Replace:
    def encrypt(self, data):
        data_array = bytearray(data)
        for i in range(len(data)):
            data_array[i] = self.key[data[i]]

        return bytes(data_array)

    def decrypt(self, data):
        data_array = bytearray(data)
        for i in range(len(data)):
            data_array[i] = self.ukey[data[i]]

        return bytes(data_array)

    def load_key(self, passphrase):
        seq = [i for i in range(256)]
        random.seed(int.from_bytes(base64.b64encode(passphrase.encode()),
                                   byteorder='little', signed=False))
        random.shuffle(seq)
        self.key = bytes(bytearray(seq))
        key_array = bytearray(self.key)
        for i in range(len(self.key)):
            key_array[self.key[i]] = i
        self.ukey = bytes(key_array)


class AES_128_GCM:
    def encrypt(self, plaindata):
        cipher = AES.new(self.key, AES.MODE_GCM)
        cipherdata, tag = cipher.encrypt_and_digest(plaindata)
        # tag len : 16bytes
        return cipher.nonce + tag + cipherdata

    def decrypt(self, data):
        cipher = AES.new(self.key, AES.MODE_GCM, data[:16])
        try:
            phaindata = cipher.decrypt_and_verify(data[32:], data[16:32])
            return phaindata
        except ValueError as e:
            raise

    def load_key(self, passphrase):
        self.passphrase = passphrase
        h = MD5.new()
        h.update(passphrase.encode())
        self.key = h.digest()


