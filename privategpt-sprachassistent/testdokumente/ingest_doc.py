import json
import urllib.request

with open("kaffeekraft_test.txt", encoding="utf-8") as f:
    text = f.read()

payload = {
    "artifact": "kaffeekraft_profil",
    "collection": "test_de",
    "input": {"type": "text", "value": text},
    "metadata": {"file_name": "kaffeekraft_test.txt", "quelle": "Testdokument RAG"},
}

req = urllib.request.Request(
    "http://localhost:8080/v1/artifacts/ingest",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=420) as resp:
        print("STATUS:", resp.status)
        print(resp.read().decode("utf-8")[:800])
except urllib.error.HTTPError as e:
    print("HTTP ERROR:", e.code)
    print(e.read().decode("utf-8")[:800])
