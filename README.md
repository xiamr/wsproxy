# wsproxy
Socks5 Proxy over WebSocket(TLS)


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
      ---------------------
      |  bzip2 (optional)  |
      ---------------------
                |
    -----------------------------
    | e.g. aes-128-gcm (optinal) |
    -----------------------------
                |
       -------------------
       |  websocket(TLS) |
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
        "mode" : "none",
        "unix_socket_path": "/var/run/wsproxy.socket",
        "serverAddress" : "www.xiamr.tk",
        "serverPort" : 443,
        "localAddress": "0.0.0.0",
        "localPort" : 8766,
        "dnsrelay": false,
        "normal_dns": "8.8.8.8",
        "enable_ssl": true,
        "keep_alive_interval": 10,
        "keep_alive_timeout" : 600,
        "loc": "c3e1b8efe92e67862bd4ca1b2bae8326f582f361",
        "compress": false
    }


server side :
    ./server.py -c config.json [-d|--debug]

local side:
    ./local.py -c config.json  [-d|--debug]