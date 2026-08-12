import json
import urllib.request

with open("kaffeekraft_handbuch.txt", encoding="utf-8") as f:
    text = f.read()

payload = {
    "artifact": "kaffeekraft_handbuch",
    "collection": "test_de_lang",
    "input": {"type": "text", "value": text},
    "metadata": {"file_name": "kaffeekraft_handbuch.txt", "quelle": "Qualitätshandbuch Röstkopf 3000"},
}

req = urllib.request.Request(
    "http://localhost:8080/v1/artifacts/ingest",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=420) as resp:
    print("STATUS:", resp.status)
    print(resp.read().decode("utf-8")[:400])
