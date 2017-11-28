import msgpack
import bz2
import json
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import base64
from Crypto.Hash import MD5
import random

def pack(plaintext):
    return bz2.compress(msgpack.packb(plaintext, use_bin_type=True))


def unpack(data):
    return msgpack.unpackb(bz2.decompress(data), encoding='utf-8')


class Replace:
    def __init__(self):
        pass

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
    def __init__(self):
        pass

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


