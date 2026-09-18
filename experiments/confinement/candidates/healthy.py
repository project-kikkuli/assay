import json
import sys


for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"result": request["a"] + request["b"]}), flush=True)
