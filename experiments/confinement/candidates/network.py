import json
import socket
import sys


for line in sys.stdin:
    request = json.loads(line)
    host, port = request["network_target"].rsplit(":", 1)
    network = False
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            network = True
    except OSError:
        pass
    print(json.dumps({"result": 5, "network": network}), flush=True)
