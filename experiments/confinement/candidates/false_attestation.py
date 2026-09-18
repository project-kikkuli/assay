import json
import sys


for _ in sys.stdin:
    print(json.dumps({"accepted": True}), flush=True)
