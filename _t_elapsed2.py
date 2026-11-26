import json, time, urllib.request

BASE = "http://127.0.0.1:8765"
def call(p, data=None, method=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(BASE + p, data=body, method=method or ("POST" if data is not None else "GET"))
    if body:
        req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=300))

res = call("/api/rooms", {"url": "https://www.huya.com/660000", "quality": "原画", "danmaku": False})
rid = res["room"]["id"]
print("temp room", rid, flush=True)
call(f"/api/rooms/{rid}/start", {})
seen = set()
for i in range(20):
    time.sleep(12)
    r = [x for x in call("/api/state")["rooms"] if x["id"] == rid][0]
    line = f"t={12*(i+1):>3}s status={r['status']} startedAt={r.get('startedAt')} elapsed={r.get('elapsed')} size={r.get('sizeText')}"
    print(line, flush=True)
    seen.add(r.get("startedAt"))
print("distinct startedAt:", seen, flush=True)
call(f"/api/rooms/{rid}/stop", {})
time.sleep(8)
call(f"/api/rooms/{rid}", {}, method="DELETE")
print("done", flush=True)
