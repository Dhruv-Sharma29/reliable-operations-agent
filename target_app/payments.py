"""External payments dependency (simulated). The agent cannot act on it; it can only observe it
through the worker. Admin endpoints exist for fault injection and for the credential-rotation
action, which plays the role of the provider's token-issuing console."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sandbox import state

app = FastAPI()
settings = {"latency_ms": 20}
tokens = {"valid": state.read_secret("payments_token"), "expired": set()}


@app.post("/charge")
async def charge(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token in tokens["expired"]:
        return JSONResponse({"error": "token_expired"}, status_code=401)
    if token != tokens["valid"]:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    await asyncio.sleep(settings["latency_ms"] / 1000)
    return {"ok": True}


@app.post("/admin/config")
async def configure(body: dict):
    settings.update({k: v for k, v in body.items() if k in settings})
    return settings


@app.post("/admin/expire")
async def expire():
    tokens["expired"].add(tokens["valid"])
    tokens["valid"] = None
    return {"expired": True}


issued_by_key: dict[str, str] = {}


@app.post("/admin/issue")
async def issue(request: Request):
    """Like real providers' idempotency keys: the same key returns the token it issued the first
    time instead of issuing (and invalidating) another one."""
    key = request.headers.get("idempotency-key")
    if key and key in issued_by_key:
        return {"token": issued_by_key[key], "replayed": True}
    tokens["valid"] = "tok_" + secrets.token_hex(12)
    tokens["issued"] = tokens.get("issued", 0) + 1
    if key:
        issued_by_key[key] = tokens["valid"]
    return {"token": tokens["valid"], "replayed": False}


@app.get("/admin/issued")
async def issued():
    """How many tokens were actually issued: lets tests check a credential was rotated once."""
    return {"issued": tokens.get("issued", 0)}


@app.get("/health")
async def health():
    return {"status": "ok"}
