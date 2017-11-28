# wsproxy
Socks5 Proxy over WebSocket

TCP over websocket ( in another word, websocket proxy)

At present, only  tcp protocol is supported and UDP is under consideration


protocol hierarchy
s
       -------------------
       |  User TCP data  |
       -------------------
                |
       -------------------
       |   messagepack   |
       ------------------
                |
       -------------------
       |      bzip2      |
       -------------------
                |
      ---------------------
      | e.g. aes-128-gcm  |
      ---------------------
                |
       -------------------
       |    websocket    |
       -------------------
                |
       -------------------
       |   system TCP    |
       -------------------
                |
       -------------------
       |   IPv4 or IPv6  |
       -------------------


require Python 3.5+
require Python module : websockets , msgpack-python , pycryptodome


confile file formation

{
    "mode" : "aes-128-gcm",
    "key" : "jnfdnfvdnvdsvdv33r932mj9&023",
    "serverAddress" : "127.0.0.1",
    "serverPort" : 8765,
    "localAddress": "0.0.0.0",
    "localPort" : 8766
}


server side :
    ./server.py -c config.json [-d|--debug]

local side:
    ./local.py -c config.json  [-d|--debug]