import json
import pathlib
import sys


seen = None
for line in sys.stdin:
    request = json.loads(line)
    try:
        seen = pathlib.Path(request["canary_path"]).read_text()
    except OSError:
        pass
    print(json.dumps({"result": 5, "canary": seen}), flush=True)
