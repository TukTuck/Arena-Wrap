#!/usr/bin/env python3
"""Proxy-Austausch für OmniRoute.

Holt freie Proxies aus öffentlichen Listen, prüft sie selbst
und schreibt funktionierende als *manuelle* Proxies nach OmniRoute —
nie in die 1proxy-/Free-Kategorie.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def resolve_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = resolve_app_dir()
STATIC_DIR = APP_DIR / "static"
HARVEST_TAG = "proxy-exchange"
TEST_URLS_HTTPS = (
    "https://api.ipify.org",
    "https://icanhazip.com",
)
TEST_URLS_HTTP = ("http://ip-api.com/line/?fields=query",)

SOURCES = [
    {
        "id": "oneproxy",
        "name": "1proxy Marketplace",
        "kind": "oneproxy",
        "url": "https://1proxy-api.aitradepulse.com/api/v1/proxies/advanced?limit=200&offset=0&is_working=true",
    },
    {
        "id": "proxyscrape-http",
        "name": "ProxyScrape HTTP",
        "kind": "text",
        "proto": "http",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=8000&country=all&ssl=all&anonymity=all",
    },
    {
        "id": "proxyscrape-socks5",
        "name": "ProxyScrape SOCKS5",
        "kind": "text",
        "proto": "socks5",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=8000&country=all",
    },
    {
        "id": "geonode",
        "name": "GeoNode",
        "kind": "geonode",
        "url": "https://proxylist.geonode.com/api/proxy-list?limit=80&page=1&sort_by=lastChecked&sort_type=desc",
    },
    {
        "id": "speedx-http",
        "name": "TheSpeedX HTTP",
        "kind": "text",
        "proto": "http",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    },
    {
        "id": "monosans-http",
        "name": "monosans HTTP",
        "kind": "text",
        "proto": "http",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    },
    {
        "id": "roosterkid-https",
        "name": "RoosterKid HTTPS",
        "kind": "text",
        "proto": "https",
        "url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    },
    {
        "id": "proxifly-https",
        "name": "Proxifly HTTPS",
        "kind": "text",
        "proto": "https",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    },
    {
        "id": "proxifly-socks5",
        "name": "Proxifly SOCKS5",
        "kind": "text",
        "proto": "socks5",
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    },
    {
        "id": "proxifly",
        "name": "Proxifly alle",
        "kind": "text",
        "proto": None,
        "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    },
]

OMNI_TYPES = ("http", "https", "socks5")

LINE_RE = re.compile(
    r"^(?:(?P<proto>https?|socks4|socks5)://)?(?:(?P<user>[^:@\s]+):(?P<pw>[^@\s]+)@)?(?P<host>[A-Za-z0-9._:-]+):(?P<port>\d{2,5})$"
)

app = FastAPI(title="OmniRoute Proxy-Austausch", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

jobs: dict[str, dict[str, Any]] = {}
settings: dict[str, Any] = {
    "omni_url": "http://127.0.0.1:20128",
    "api_key": "",
}

# Letzter/laufender Austausch-Job — für GET /api/job-status und Server-Logging.
job_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "phase": None,
    "phase_label": None,
    "error": None,
    "counts": {},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def proxy_key(p: dict[str, Any]) -> str:
    return f"{p.get('type', 'http')}://{p['host']}:{int(p['port'])}"


def parse_proxy_line(raw: str, default_proto: str | None = None) -> dict[str, Any] | None:
    line = raw.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None
    m = LINE_RE.match(line)
    if not m:
        return None
    proto = (m.group("proto") or default_proto or "http").lower()
    if proto == "socks4":
        return None
    if proto not in OMNI_TYPES:
        return None
    port = int(m.group("port"))
    if port < 1 or port > 65535:
        return None
    host = m.group("host")
    if host.startswith("[") or host.count(":") > 1:
        return None
    return {
        "type": proto,
        "host": host,
        "port": port,
        "username": m.group("user") or "",
        "password": m.group("pw") or "",
        "country": None,
        "quality": None,
        "anonymity": None,
        "source": None,
    }


def merge_proxy(store: dict[str, dict[str, Any]], item: dict[str, Any], source: str) -> None:
    item = dict(item)
    item["source"] = source
    key = proxy_key(item)
    if key in store:
        old = store[key]
        for field in ("country", "quality", "anonymity"):
            if not old.get(field) and item.get(field):
                old[field] = item[field]
        return
    store[key] = item


async def fetch_source(client: httpx.AsyncClient, src: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        r = await client.get(src["url"], timeout=20.0, follow_redirects=True)
        r.raise_for_status()
    except Exception as exc:
        return src["id"], [], str(exc)

    found: list[dict[str, Any]] = []
    kind = src["kind"]

    if kind == "oneproxy":
        data = r.json()
        for row in data.get("proxies") or []:
            proto = (row.get("protocol") or "http").lower()
            if proto == "https":
                proto = "http"
            if proto == "socks4":
                proto = "socks5"
            if proto not in ("http", "socks5"):
                continue
            if not row.get("ip") or not row.get("port"):
                continue
            found.append(
                {
                    "type": proto,
                    "host": row["ip"],
                    "port": int(row["port"]),
                    "username": "",
                    "password": "",
                    "country": row.get("country_code"),
                    "quality": row.get("quality_score"),
                    "anonymity": row.get("anonymity"),
                    "source": src["id"],
                }
            )
    elif kind == "geonode":
        data = r.json()
        for row in data.get("data") or []:
            protocols = [p.lower() for p in (row.get("protocols") or [])]
            if "socks5" in protocols:
                proto = "socks5"
            elif "https" in protocols:
                proto = "https"
            else:
                proto = "http"
            if not row.get("ip") or not row.get("port"):
                continue
            found.append(
                {
                    "type": proto,
                    "host": row["ip"],
                    "port": int(row["port"]),
                    "username": "",
                    "password": "",
                    "country": row.get("country"),
                    "quality": None,
                    "anonymity": row.get("anonymityLevel"),
                    "source": src["id"],
                }
            )
    else:
        for line in r.text.splitlines():
            item = parse_proxy_line(line, src.get("proto"))
            if item:
                item["source"] = src["id"]
                found.append(item)

    return src["id"], found, None


def proxy_url(p: dict[str, Any], proto: str | None = None) -> str:
    scheme = proto or p.get("type") or "http"
    auth = ""
    if p.get("username"):
        auth = f"{p['username']}:{p.get('password') or ''}@"
    return f"{scheme}://{auth}{p['host']}:{int(p['port'])}"


def schemes_to_try(declared: str) -> list[str]:
    """OmniRoute-Typen. Listen nennen oft 'https', meinen aber HTTP+CONNECT."""
    declared = (declared or "http").lower()
    if declared == "socks5":
        return ["socks5"]
    if declared == "https":
        return ["https", "http"]
    return ["http"]


async def probe_one(p: dict[str, Any], timeout: float) -> dict[str, Any]:
    result = {
        **p,
        "ok": False,
        "https_ok": False,
        "exit_ip": None,
        "latency_ms": None,
        "error": None,
        "checked_at": now_iso(),
        "declared_type": p.get("type") or "http",
    }
    started = time.perf_counter()
    last_error = "keine Antwort"

    for scheme in schemes_to_try(p.get("type") or "http"):
        url = proxy_url(p, scheme)
        try:
            async with httpx.AsyncClient(
                proxy=url, timeout=timeout, follow_redirects=True, verify=False
            ) as client:
                for target in TEST_URLS_HTTPS:
                    try:
                        resp = await client.get(target)
                        if resp.status_code < 500 and resp.text.strip():
                            result["ok"] = True
                            result["https_ok"] = True
                            result["type"] = scheme
                            result["exit_ip"] = resp.text.strip().split()[0][:64]
                            result["latency_ms"] = int((time.perf_counter() - started) * 1000)
                            result["error"] = None
                            return result
                    except Exception as exc:
                        last_error = str(exc)[:180]
                for target in TEST_URLS_HTTP:
                    try:
                        resp = await client.get(target)
                        if resp.status_code < 500 and resp.text.strip():
                            result["ok"] = True
                            result["https_ok"] = False
                            result["type"] = scheme
                            result["exit_ip"] = resp.text.strip().split()[0][:64]
                            result["latency_ms"] = int((time.perf_counter() - started) * 1000)
                            result["error"] = "nur Klartext-HTTP — OmniRoute braucht TLS-Ziele"
                            return result
                    except Exception as exc:
                        last_error = str(exc)[:180]
        except Exception as exc:
            last_error = str(exc)[:180]

    result["error"] = last_error
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


async def probe_many(
    proxies: list[dict[str, Any]],
    timeout: float,
    concurrency: int,
    on_one=None,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, Any]] = []

    async def run(p: dict[str, Any]) -> None:
        async with sem:
            try:
                # Harte Deadline: kein einzelner Check darf ewig hängen.
                checked = await asyncio.wait_for(
                    probe_one(p, timeout), timeout=max(15.0, timeout * 7 + 5)
                )
            except asyncio.TimeoutError:
                checked = {
                    **p,
                    "ok": False,
                    "https_ok": False,
                    "exit_ip": None,
                    "latency_ms": None,
                    "error": "Prüfung abgebrochen (Deadline überschritten)",
                    "checked_at": now_iso(),
                    "declared_type": p.get("type") or "http",
                }
            results.append(checked)
            if on_one:
                await on_one(checked)

    await asyncio.gather(*(run(p) for p in proxies))
    results.sort(key=lambda x: (not x["https_ok"], not x["ok"], x.get("latency_ms") or 9_999))
    return results


def omni_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = (settings.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key
    return headers


def omni_base() -> str:
    return (settings.get("omni_url") or "http://127.0.0.1:20128").rstrip("/")


async def omni_request(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"{omni_base()}{path}"
    timeout = kwargs.pop("timeout", 20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await client.request(method, url, headers=omni_headers(), **kwargs)


def unwrap_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("proxies", "items", "data", "results", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def is_harvest(p: dict[str, Any]) -> bool:
    notes = str(p.get("notes") or "")
    name = str(p.get("name") or "")
    return HARVEST_TAG in notes or name.startswith("px-")


def normalize_omni(p: dict[str, Any], bucket: str) -> dict[str, Any]:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "type": (p.get("type") or p.get("protocol") or "http").lower(),
        "host": p.get("host") or p.get("ip"),
        "port": p.get("port"),
        "username": p.get("username") or "",
        "password": p.get("password") or "",
        "region": p.get("region") or p.get("countryCode") or p.get("country_code"),
        "status": p.get("status"),
        "source": p.get("source") or bucket,
        "notes": p.get("notes") or "",
        "quality": p.get("quality_score") or p.get("qualityScore"),
        "latency_ms": p.get("latency_ms") or p.get("latencyMs"),
        "anonymity": p.get("anonymity"),
        "bucket": bucket,
        "harvest": is_harvest(p) or bucket == "harvest",
    }


async def list_management_proxies() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = await omni_request("GET", f"/api/v1/management/proxies?limit=100&offset={offset}")
        if resp.status_code == 401:
            raise HTTPException(401, "OmniRoute verlangt Auth. API-Key mit manage-Scope eintragen.")
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"OmniRoute Registry: {resp.text[:300]}")
        batch = unwrap_list(resp.json())
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        if offset > 5000:
            break
    return out


async def list_oneproxy() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    proxies: list[dict[str, Any]] = []
    stats = None
    try:
        resp = await omni_request("GET", "/api/settings/oneproxy?action=stats")
        if resp.status_code < 400:
            stats = resp.json()
    except Exception:
        stats = None
    offset = 0
    while True:
        resp = await omni_request("GET", f"/api/settings/oneproxy?limit=100&offset={offset}")
        if resp.status_code >= 400:
            break
        batch = unwrap_list(resp.json())
        if not batch:
            break
        proxies.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        if offset > 5000:
            break
    return proxies, stats


async def list_providers() -> list[dict[str, Any]]:
    """Installierte Provider-Connections via GET /api/providers."""
    resp = await omni_request("GET", "/api/providers?limit=200")
    if resp.status_code == 401:
        raise HTTPException(401, "OmniRoute verlangt Auth. API-Key mit manage-Scope eintragen.")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"OmniRoute Provider-API: {resp.text[:300]}")
    data = resp.json()
    return data.get("connections") or []


def proxy_latency_ms(p: dict[str, Any]) -> int:
    m = re.search(r"latency=(\d+)ms", str(p.get("notes") or ""))
    return int(m.group(1)) if m else 9_999


async def assign_proxy_to_providers(
    proxy_id: str, scope: str, scope_ids: list[str]
) -> httpx.Response:
    """PUT bulk-assign; falls die Version POST erwartet (Doku), Retry mit POST."""
    payload = {"scope": scope, "scopeIds": scope_ids, "proxyId": proxy_id}
    resp = await omni_request("PUT", "/api/v1/management/proxies/bulk-assign", json=payload)
    if resp.status_code == 405:
        resp = await omni_request("POST", "/api/v1/management/proxies/bulk-assign", json=payload)
    return resp


class ConnectBody(BaseModel):
    omni_url: str = Field(default="http://127.0.0.1:20128")
    api_key: str = ""


def clean_types(raw: list[str] | None) -> list[str]:
    wanted = [t.lower() for t in (raw or []) if t and t.lower() in OMNI_TYPES]
    return wanted or ["http", "https"]


class HarvestBody(BaseModel):
    sources: list[str] | None = None
    types: list[str] | None = None
    limit: int = 180


class CheckBody(BaseModel):
    proxies: list[dict[str, Any]] | None = None
    types: list[str] | None = None
    timeout: float = 7.0
    concurrency: int = 18
    limit: int = 120


class ExchangeBody(BaseModel):
    timeout: float = 7.0
    concurrency: int = 16
    harvest_limit: int = 140
    max_push: int = 25
    remove_dead_harvest: bool = True
    push_live: bool = True
    check_oneproxy: bool = True
    sources: list[str] | None = None
    types: list[str] | None = None


class AssignBody(BaseModel):
    providers: list[str] | None = None
    count: int = 3


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(f"{APP_DIR}/static/index.html")


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    return {
        "harvest_tag": HARVEST_TAG,
        "sources": [{"id": s["id"], "name": s["name"]} for s in SOURCES],
        "settings": {"omni_url": settings["omni_url"], "has_key": bool(settings["api_key"])},
    }


@app.post("/api/connect")
async def connect(body: ConnectBody) -> dict[str, Any]:
    settings["omni_url"] = body.omni_url.strip() or "http://127.0.0.1:20128"
    settings["api_key"] = body.api_key.strip()
    parsed = urlparse(settings["omni_url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Ungültige OmniRoute-URL")

    reachable = False
    auth_ok = None
    detail = ""
    version = None
    try:
        resp = await omni_request("GET", "/api/v1/management/proxies?limit=1", timeout=6.0)
        reachable = True
        auth_ok = resp.status_code != 401
        if resp.status_code < 400:
            detail = "Registry erreichbar"
        else:
            detail = f"HTTP {resp.status_code}: {resp.text[:160]}"
    except Exception as exc:
        detail = str(exc)
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                ping = await client.get(f"{omni_base()}/")
                reachable = ping.status_code < 500
                detail = f"Host antwortet ({ping.status_code}), Management-API nicht."
        except Exception:
            pass

    return {
        "ok": bool(reachable and auth_ok),
        "reachable": reachable,
        "auth_ok": auth_ok,
        "detail": detail,
        "version": version,
        "omni_url": settings["omni_url"],
    }


@app.get("/api/omni/inventory")
async def inventory() -> dict[str, Any]:
    try:
        registry = await list_management_proxies()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"OmniRoute nicht erreichbar: {exc}") from exc

    oneproxy_raw, stats = [], None
    try:
        oneproxy_raw, stats = await list_oneproxy()
    except Exception:
        oneproxy_raw, stats = [], None

    items = [normalize_omni(p, "registry") for p in registry if p.get("host") and p.get("port")]
    oneproxy_keys = {proxy_key(normalize_omni(p, "oneproxy")) for p in oneproxy_raw if p.get("host") or p.get("ip")}

    harvest, manual, oneproxy_in_reg = [], [], []
    for item in items:
        src = str(item.get("source") or "").lower()
        if src == "oneproxy" or proxy_key(item) in oneproxy_keys:
            item["bucket"] = "oneproxy"
            oneproxy_in_reg.append(item)
        elif item["harvest"]:
            item["bucket"] = "harvest"
            harvest.append(item)
        else:
            item["bucket"] = "manual"
            manual.append(item)

    extra_oneproxy = []
    seen = {proxy_key(p) for p in oneproxy_in_reg}
    for p in oneproxy_raw:
        n = normalize_omni(p, "oneproxy")
        if n.get("host") and n.get("port") and proxy_key(n) not in seen:
            extra_oneproxy.append(n)

    return {
        "harvest": harvest,
        "manual": manual,
        "oneproxy": oneproxy_in_reg + extra_oneproxy,
        "oneproxy_stats": stats,
        "counts": {
            "harvest": len(harvest),
            "manual": len(manual),
            "oneproxy": len(oneproxy_in_reg) + len(extra_oneproxy),
            "registry": len(items),
        },
    }


@app.post("/api/harvest")
async def harvest(body: HarvestBody) -> dict[str, Any]:
    wanted = set(body.sources) if body.sources else {s["id"] for s in SOURCES}
    types = set(clean_types(body.types))
    store: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={"User-Agent": "proxy-exchange/1.0"}) as client:
        tasks = [fetch_source(client, s) for s in SOURCES if s["id"] in wanted]
        for sid, found, err in await asyncio.gather(*tasks):
            kept = 0
            for item in found:
                if item.get("type") not in types:
                    continue
                merge_proxy(store, item, sid)
                kept += 1
            reports.append({"id": sid, "found": kept, "raw": len(found), "error": err})

    proxies = list(store.values())[: max(20, min(body.limit, 400))]
    return {"count": len(proxies), "proxies": proxies, "sources": reports, "types": sorted(types)}


@app.post("/api/check")
async def check(body: CheckBody) -> dict[str, Any]:
    proxies = body.proxies or []
    if not proxies:
        raise HTTPException(400, "Keine Proxies zum Prüfen")
    types = set(clean_types(body.types))
    proxies = [p for p in proxies if (p.get("type") or "http") in types]
    proxies = proxies[: max(1, min(body.limit, 300))]
    checked = await probe_many(proxies, body.timeout, body.concurrency)
    live = [p for p in checked if p["https_ok"]]
    http_only = [p for p in checked if p["ok"] and not p["https_ok"]]
    dead = [p for p in checked if not p["ok"]]
    return {
        "checked": len(checked),
        "https_ok": len(live),
        "http_only": len(http_only),
        "dead": len(dead),
        "proxies": checked,
    }


@app.post("/api/assign-providers")
async def assign_providers(body: AssignBody) -> dict[str, Any]:
    """Ordnet die besten eigenen px-*-Proxies allen installierten Providern zu
    (scope=provider). Prüft zur Laufzeit, ob die OmniRoute-Version mehrere
    Proxies pro Scope (Pool) via API zulässt; sonst gilt 1 Proxy pro Provider.
    """
    if not (settings.get("api_key") or "").strip():
        raise HTTPException(400, "Zuerst mit OmniRoute verbinden (API-Key mit manage-Scope eintragen).")
    count = max(1, min(body.count, 8))

    connections = await list_providers()
    if body.providers:
        provider_ids = [p.strip().lower() for p in body.providers if p.strip()]
    else:
        seen: set[str] = set()
        provider_ids = []
        for c in connections:
            pid = str(c.get("provider") or "").strip().lower()
            if pid and pid not in seen:
                seen.add(pid)
                provider_ids.append(pid)
    if not provider_ids:
        raise HTTPException(400, "Keine installierten Provider gefunden (GET /api/providers leer).")

    registry = await list_management_proxies()
    harvest = [
        p
        for p in registry
        if is_harvest(p)
        and str(p.get("status") or "active") != "inactive"
        and p.get("id")
        and p.get("host")
        and p.get("port")
    ]
    harvest.sort(key=proxy_latency_ms)
    best = harvest[:count]
    if not best:
        raise HTTPException(400, "Keine eigenen Austausch-Proxies (px-*) im Register gefunden.")

    def short(p: dict[str, Any]) -> str:
        return f"{p['host']}:{p['port']}"

    results: dict[str, Any] = {
        "providers": provider_ids,
        "best": [short(p) for p in best],
        "assigned": {},
        "pool_mode": None,
        "pool_mode_reason": None,
    }

    # 1) Besten Proxy jedem Provider zuordnen (dokumentierte Semantik).
    for pid in provider_ids:
        resp = await assign_proxy_to_providers(best[0]["id"], "provider", [pid])
        results["assigned"][pid] = {
            "proxy": short(best[0]),
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "detail": resp.text[:200] if resp.status_code >= 400 else "",
        }

    # 2) Mehrfach-Zuordnung (Pool) ausprobieren: klappt es, bekommt jeder
    #    Provider bis zu `count` Proxies; sonst besten Proxy wiederherstellen.
    if len(best) > 1:
        probe_pid = provider_ids[0]
        probe = await assign_proxy_to_providers(best[1]["id"], "provider", [probe_pid])
        assign_resp = await omni_request(
            "GET",
            f"/api/v1/management/proxies/assignments?scope=provider&scope_id={probe_pid}&limit=50",
        )
        try:
            items = assign_resp.json().get("items") or []
        except Exception:
            items = []
        best_ids = {p["id"] for p in best}
        mine = [it for it in items if it.get("proxyId") in best_ids]
        if probe.status_code < 400 and len(mine) >= 2:
            results["pool_mode"] = True
            for pid in provider_ids:
                pool = []
                for proxy in best[1:]:
                    r = await assign_proxy_to_providers(proxy["id"], "provider", [pid])
                    pool.append({"proxy": short(proxy), "ok": r.status_code < 400, "status": r.status_code})
                results["assigned"][pid]["pool"] = pool
        else:
            results["pool_mode"] = False
            await assign_proxy_to_providers(best[0]["id"], "provider", [probe_pid])
            results["assigned"][probe_pid]["proxy"] = short(best[0])
            results["pool_mode_reason"] = (
                "Diese OmniRoute-Version ordnet via API nur 1 Proxy pro Scope zu (Replace-Semantik). "
                "Für einen Mehrfach-Pool im OmniRoute-Dashboard zuordnen."
            )

    # 3) Nachprüfen: was steht jetzt tatsächlich in OmniRoute (scope=provider)?
    verified: dict[str, Any] = {}
    by_id = {p["id"]: short(p) for p in harvest}
    for pid in provider_ids:
        ar = await omni_request(
            "GET",
            f"/api/v1/management/proxies/assignments?scope=provider&scope_id={pid}&limit=50",
        )
        try:
            items = ar.json().get("items") or []
        except Exception:
            items = []
        verified[pid] = [
            {"proxy_id": it.get("proxyId"), "proxy": by_id.get(it.get("proxyId"), "?")}
            for it in items
            if it.get("scopeId") == pid
        ]
    results["verified"] = verified

    results["counts"] = {
        "providers": len(provider_ids),
        "proxies_used": len(best) if results["pool_mode"] else 1,
    }
    return results


class ProviderStatusBody(BaseModel):
    ids: list[str]
    is_active: bool = False


@app.get("/api/omni/providers")
async def omni_providers() -> dict[str, Any]:
    """Liest alle installierten Provider-Connections aus OmniRoute (read-only)."""
    if not (settings.get("api_key") or "").strip():
        raise HTTPException(400, "Zuerst mit OmniRoute verbinden (API-Key mit manage-Scope eintragen).")
    connections = await list_providers()
    return {"total": len(connections), "connections": connections}


@app.post("/api/omni/provider-status")
async def omni_provider_status(body: ProviderStatusBody) -> dict[str, Any]:
    """Aktiviert/deaktiviert Provider-Connections via PATCH /api/providers."""
    if not body.ids:
        raise HTTPException(400, "Keine Connection-IDs angegeben.")
    if not (settings.get("api_key") or "").strip():
        raise HTTPException(400, "Zuerst mit OmniRoute verbinden (API-Key mit manage-Scope eintragen).")
    resp = await omni_request("PATCH", "/api/providers", json={"ids": body.ids, "isActive": body.is_active})
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"OmniRoute: {resp.text[:300]}")
    return resp.json()


@app.post("/api/exchange")
async def exchange(body: ExchangeBody) -> StreamingResponse:
    async def gen():
        def ev(kind: str, payload: dict[str, Any]) -> str:
            # Server-seitiges Laufzeit-Log: jede Phase, jedes Log und jedes
            # Ergebnis-Event landet in der Konsole + job_state (Diagnose).
            if kind == "phase":
                job_state["phase"] = payload.get("step")
                job_state["phase_label"] = payload.get("label")
                print(f"[job] {now_iso()} phase={payload.get('step')} {payload.get('label')}", flush=True)
            elif kind in ("log", "error"):
                print(f"[job] {now_iso()} {kind}: {payload.get('text') or payload.get('message')}", flush=True)
            elif kind in ("removed", "pushed", "push_fail", "done"):
                print(f"[job] {now_iso()} {kind}: {json.dumps(payload, ensure_ascii=False)[:300]}", flush=True)
            return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        job_state.update(
            running=True, started_at=now_iso(), finished_at=None,
            phase=None, phase_label=None, error=None, counts={},
        )
        summary: dict[str, Any] = {
            "started_at": now_iso(),
            "removed": [],
            "pushed": [],
            "failed_push": [],
            "oneproxy_dead": [],
            "oneproxy_live": [],
            "harvested": 0,
            "tested_new": 0,
            "live_new": 0,
            "error": None,
        }

        try:
            yield ev("phase", {"step": "inventory", "label": "OmniRoute-Bestand laden"})
            inv = await inventory()
            harvest_pool = inv["harvest"]
            yield ev(
                "log",
                {
                    "text": (
                        f"Bestand: {inv['counts']['harvest']} Austausch-Proxies, "
                        f"{inv['counts']['manual']} eigene manuelle, "
                        f"{inv['counts']['oneproxy']} in 1proxy (nur lesen)."
                    )
                },
            )

            if harvest_pool:
                yield ev("phase", {"step": "check-harvest", "label": "Eigene Austausch-Proxies prüfen"})
                checked_old = await probe_many(harvest_pool, body.timeout, body.concurrency)
                dead_old = [p for p in checked_old if not p["https_ok"]]
                live_old = [p for p in checked_old if p["https_ok"]]
                yield ev("log", {"text": f"Austausch-Pool: {len(live_old)} lebendig, {len(dead_old)} tot."})
                if body.remove_dead_harvest:
                    for p in dead_old:
                        if not p.get("id"):
                            continue
                        resp = await omni_request("DELETE", f"/api/v1/management/proxies?id={p['id']}&force=1")
                        ok = resp.status_code < 400
                        entry = {"id": p["id"], "host": p["host"], "port": p["port"], "ok": ok}
                        summary["removed"].append(entry)
                        yield ev("removed", entry)

            if body.check_oneproxy and inv["oneproxy"]:
                yield ev("phase", {"step": "audit-oneproxy", "label": "1proxy-Bestand nur prüfen, nichts schreiben"})
                sample = inv["oneproxy"][:80]
                checked_op = await probe_many(sample, body.timeout, body.concurrency)
                summary["oneproxy_live"] = [
                    {"host": p["host"], "port": p["port"], "ms": p.get("latency_ms")}
                    for p in checked_op
                    if p["https_ok"]
                ]
                summary["oneproxy_dead"] = [
                    {"host": p["host"], "port": p["port"], "error": p.get("error")}
                    for p in checked_op
                    if not p["https_ok"]
                ]
                yield ev(
                    "log",
                    {
                        "text": (
                            f"1proxy-Audit ({len(sample)}): "
                            f"{len(summary['oneproxy_live'])} HTTPS ok, "
                            f"{len(summary['oneproxy_dead'])} tot. "
                            "Tote werden NICHT in der Free-Kategorie ersetzt."
                        )
                    },
                )

            yield ev("phase", {"step": "harvest", "label": "Freie Listen aus dem Netz holen"})
            types = set(clean_types(body.types))
            harvested = await harvest(
                HarvestBody(sources=body.sources, types=body.types, limit=body.harvest_limit)
            )
            summary["harvested"] = harvested["count"]
            existing_keys = {
                proxy_key(p)
                for group in (inv["harvest"], inv["manual"], inv["oneproxy"])
                for p in group
                if p.get("host") and p.get("port")
            }
            candidates = [
                p
                for p in harvested["proxies"]
                if (p.get("type") or "http") in types and proxy_key(p) not in existing_keys
            ]
            yield ev(
                "log",
                {
                    "text": (
                        f"{harvested['count']} Kandidaten, {len(candidates)} neu gegenüber OmniRoute "
                        f"(Typen: {', '.join(sorted(types))})."
                    )
                },
            )

            yield ev("phase", {"step": "check-new", "label": "Neue Kandidaten selbst prüfen"})
            checked_new = await probe_many(candidates, body.timeout, body.concurrency)
            summary["tested_new"] = len(checked_new)
            live_new = [
                p
                for p in checked_new
                if p["https_ok"] and (p.get("declared_type") or p.get("type") or "http") in types
            ]
            summary["live_new"] = len(live_new)
            yield ev("log", {"text": f"Neue Prüfung: {len(live_new)} mit HTTPS lebendig von {len(checked_new)}."})

            if body.push_live:
                yield ev("phase", {"step": "push", "label": "Lebendige als manuelle Proxies eintragen"})
                to_push = live_new[: max(1, min(body.max_push, 80))]
                for p in to_push:
                    country = (p.get("country") or "xx").lower()
                    name = f"px-{country}-{p['host']}"
                    payload = {
                        "name": name[:80],
                        "type": p.get("type") or "http",
                        "host": p["host"],
                        "port": int(p["port"]),
                        "username": p.get("username") or "",
                        "password": p.get("password") or "",
                        "region": (p.get("country") or "")[:8] or None,
                        "notes": f"{HARVEST_TAG} https_ok latency={p.get('latency_ms')}ms exit={p.get('exit_ip') or '-'}",
                    }
                    resp = await omni_request("POST", "/api/v1/management/proxies", json=payload)
                    if resp.status_code < 400:
                        created = resp.json() if resp.content else {}
                        entry = {
                            "id": created.get("id") if isinstance(created, dict) else None,
                            "host": p["host"],
                            "port": p["port"],
                            "type": p.get("type"),
                            "ms": p.get("latency_ms"),
                            "exit_ip": p.get("exit_ip"),
                        }
                        summary["pushed"].append(entry)
                        yield ev("pushed", entry)
                    else:
                        fail = {
                            "host": p["host"],
                            "port": p["port"],
                            "error": resp.text[:180],
                            "status": resp.status_code,
                        }
                        summary["failed_push"].append(fail)
                        yield ev("push_fail", fail)

            summary["finished_at"] = now_iso()
            yield ev("done", summary)
        except HTTPException as exc:
            summary["error"] = exc.detail
            yield ev("error", {"message": exc.detail})
            yield ev("done", summary)
        except Exception as exc:
            summary["error"] = str(exc)
            yield ev("error", {"message": str(exc)})
            yield ev("done", summary)
        finally:
            job_state["running"] = False
            job_state["finished_at"] = now_iso()
            job_state["error"] = summary.get("error")
            job_state["counts"] = {
                "removed": len(summary.get("removed") or []),
                "pushed": len(summary.get("pushed") or []),
                "harvested": summary.get("harvested"),
                "tested_new": summary.get("tested_new"),
                "live_new": summary.get("live_new"),
                "oneproxy_dead": len(summary.get("oneproxy_dead") or []),
            }
            print(f"[job] {now_iso()} FERTIG error={job_state['error']!r} counts={job_state['counts']}", flush=True)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/job-status")
async def job_status() -> dict[str, Any]:
    """Aktueller/letzter Austausch-Job (Phase, Zähler, Fehler) — zum Diagnostizieren."""
    return dict(job_state)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8765"))
    # Windows: nur localhost — weniger Firewall-Dialoge.
    # In der Vorschau/Linux: 0.0.0.0, überschreibbar per HOST=.
    default_host = "127.0.0.1" if sys.platform == "win32" else "0.0.0.0"
    host = os.environ.get("HOST", default_host)
    url = f"http://127.0.0.1:{port}"
    print(f"Schaltwerk: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")
