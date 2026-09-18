import json
import sys


for _ in sys.stdin:
    print(json.dumps({"result": 6}), flush=True)
