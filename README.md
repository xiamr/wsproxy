# wsproxy
Socks5 Proxy over WebSocket


TCP proxy and UDP proxy are both supported

Unlike traditional shadowsocks, udp packets are encode in TCP stream 

DNS tunnel is added

protocol hierarchy
        
       -------------------
       |  User TCP or UDP |
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


require Python 3.6+
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