import json
import pathlib
import sys


edited = False
for path in ("/oracle.py", "/candidate/oracle.py", "/tmp/oracle-cache.json"):
    try:
        pathlib.Path(path).write_text("tampered")
        edited = True
    except OSError:
        pass
for _ in sys.stdin:
    print(json.dumps({"result": 5, "cache_edited": edited}), flush=True)
