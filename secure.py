import msgpack
import bz2
import json


def pack(string):
    return bz2.compress(msgpack.packb(string, use_bin_type=True))


def unpack(data):
    return msgpack.unpackb(bz2.decompress(data), encoding='utf-8')


class Secure:
    def __init__(self):
        pass

    def Encrypt(self, data):
        data_array = bytearray(data)
        for i in range(len(data)):
            data_array[i] = self.key[data[i]]

        return bytes(data_array)

    def Decrypt(self, data):
        data_array = bytearray(data)
        for i in range(len(data)):
            data_array[i] = self.ukey[data[i]]

        return bytes(data_array)

    def loadKey(self, key_list):
        self.key = bytes(bytearray(key_list))
        key_array = bytearray(self.key)
        for i in range(len(self.key)):
            key_array[self.key[i]] = i
        self.ukey = bytes(key_array)
