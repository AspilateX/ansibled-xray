from __future__ import annotations

import base64
import errno
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import docker
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from scalar_fastapi import get_scalar_api_reference

DEFAULT_FLOW = "xtls-rprx-vision"
DEFAULT_FP = "chrome"
DEFAULT_PROTOCOL = "vless"
SUPPORTED_PROTOCOLS = {"vless", "vmess"}
DEFAULT_VMESS_PORT = 10086

USERS_FILE = Path(os.getenv("XRAY_USERS_FILE", "/data/users.json"))
SETTINGS_FILE = Path(os.getenv("XRAY_SETTINGS_FILE", "/data/api_settings.json"))
CONFIG_FILE = Path(os.getenv("XRAY_CONFIG_FILE", "/data/config.json"))
XRAY_CONTAINER_NAME = os.getenv("XRAY_CONTAINER_NAME", "xray")
XRAY_API_TOKEN = os.getenv("XRAY_API_TOKEN", "")

app = FastAPI(
    title="Xray User API",
    version="1.0.0",
    description="Manage Xray users and auto-apply Xray config",
)
state_lock = threading.Lock()


def _authorize(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if XRAY_API_TOKEN and x_api_key != XRAY_API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    id: str | None = None
    protocol: str = Field(default=DEFAULT_PROTOCOL, min_length=1, max_length=16)
    flow: str = Field(default=DEFAULT_FLOW, min_length=1, max_length=128)


class UserUpdate(BaseModel):
    new_name: str | None = Field(default=None, min_length=1, max_length=128)
    id: str | None = None
    protocol: str | None = Field(default=None, min_length=1, max_length=16)
    flow: str | None = Field(default=None, min_length=1, max_length=128)


class UserOut(BaseModel):
    name: str
    id: str
    protocol: str
    flow: str | None = None


class UrlOut(BaseModel):
    name: str
    url: str


def _validate_uuid(value: str) -> None:
    uuid.UUID(value)


def _validate_protocol(value: str) -> str:
    protocol = value.strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported protocol '{value}'. Supported: {', '.join(sorted(SUPPORTED_PROTOCOLS))}",
        )
    return protocol


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {path}: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        tmp_path.replace(path)
    except OSError as exc:
        # File-level bind mounts can make atomic replace impossible (EXDEV).
        if exc.errno != errno.EXDEV:
            raise
        path.write_text(payload, encoding="utf-8")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_users() -> list[dict[str, str]]:
    raw = _read_json(USERS_FILE)
    if not isinstance(raw, list):
        raise HTTPException(status_code=500, detail="users.json must be an array")

    users: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=500, detail="Each user must be an object")
        name = str(item.get("name", "")).strip()
        user_id = str(item.get("id", "")).strip()
        protocol = _validate_protocol(str(item.get("protocol", DEFAULT_PROTOCOL)))
        flow = str(item.get("flow", DEFAULT_FLOW)).strip() if protocol == "vless" else ""
        if protocol == "vless":
            flow = flow or DEFAULT_FLOW
        if not name or not user_id:
            raise HTTPException(status_code=500, detail="Each user needs name and id")
        try:
            _validate_uuid(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid UUID in users.json: {user_id}") from exc
        user: dict[str, str] = {"name": name, "id": user_id, "protocol": protocol}
        if protocol == "vless":
            user["flow"] = flow
        users.append(user)

    _assert_uniques(users)
    return users


def _save_users(users: list[dict[str, str]]) -> None:
    _write_json(USERS_FILE, users)


def _assert_uniques(users: list[dict[str, str]]) -> None:
    names = [u["name"] for u in users]
    ids = [u["id"] for u in users]
    if len(names) != len(set(names)):
        raise HTTPException(status_code=400, detail="User names must be unique")
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="User IDs must be unique")


def _load_settings() -> dict[str, str]:
    raw = _read_json(SETTINGS_FILE)
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="api_settings.json must be an object")

    required = ["xray_domain"]
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing settings: {', '.join(missing)}")

    settings = {k: str(v) for k, v in raw.items()}
    settings.setdefault("xray_vmess_port", str(DEFAULT_VMESS_PORT))
    return settings


def _build_xray_config(settings: dict[str, str], users: list[dict[str, str]]) -> dict[str, Any]:
    inbounds: list[dict[str, Any]] = []
    vless_users = [u for u in users if u["protocol"] == "vless"]
    vmess_users = [u for u in users if u["protocol"] == "vmess"]

    if vless_users:
        required = [
            "xray_port",
            "reality_private_key",
            "short_id",
        ]
        missing = [key for key in required if not settings.get(key)]
        if missing:
            raise HTTPException(status_code=500, detail=f"Missing VLESS settings: {', '.join(missing)}")

        clients = [{"id": u["id"], "flow": u.get("flow", DEFAULT_FLOW)} for u in vless_users]
        inbounds.append(
            {
                "port": int(settings["xray_port"]),
                "protocol": "vless",
                "settings": {
                    "clients": clients,
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": f"{settings['xray_domain']}:443",
                        "xver": 0,
                        "serverNames": [settings["xray_domain"]],
                        "privateKey": settings["reality_private_key"],
                        "shortIds": [settings["short_id"]],
                    },
                },
            }
        )

    if vmess_users:
        clients = [{"id": u["id"], "alterId": 0} for u in vmess_users]
        inbounds.append(
            {
                "port": int(settings["xray_vmess_port"]),
                "protocol": "vmess",
                "settings": {
                    "clients": clients,
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "none",
                },
            }
        )

    if not inbounds:
        raise HTTPException(status_code=500, detail="No valid users/protocols to build inbounds")

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [{"protocol": "freedom"}],
    }


def _restart_xray() -> None:
    try:
        client = docker.from_env()
        container = client.containers.get(XRAY_CONTAINER_NAME)
        container.restart(timeout=10)
    except Exception as exc:  # pragma: no cover - runtime integration
        raise HTTPException(status_code=500, detail=f"Failed to restart container '{XRAY_CONTAINER_NAME}': {exc}") from exc


def _apply(users: list[dict[str, str]]) -> None:
    settings = _load_settings()
    config = _build_xray_config(settings, users)
    _write_json(CONFIG_FILE, config)
    _restart_xray()


def _find_user(users: list[dict[str, str]], name: str) -> dict[str, str] | None:
    return next((u for u in users if u["name"] == name), None)


def _build_url(user: dict[str, str], fp: str = DEFAULT_FP) -> str:
    settings = _load_settings()
    protocol = user["protocol"]
    if protocol == "vmess":
        vmess_payload = {
            "v": "2",
            "ps": user["name"],
            "add": settings["xray_domain"],
            "port": settings["xray_vmess_port"],
            "id": user["id"],
            "aid": "0",
            "scy": "auto",
            "net": "tcp",
            "type": "none",
            "host": "",
            "path": "",
            "tls": "",
        }
        encoded = base64.b64encode(json.dumps(vmess_payload, ensure_ascii=True).encode("utf-8")).decode("ascii")
        return f"vmess://{encoded}"

    missing = [key for key in ["xray_port", "reality_public_key", "short_id"] if not settings.get(key)]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing VLESS settings: {', '.join(missing)}")

    params = [
        ("encryption", "none"),
        ("flow", user.get("flow", DEFAULT_FLOW)),
        ("security", "reality"),
        ("sni", settings["xray_domain"]),
        ("fp", fp),
        ("pbk", settings["reality_public_key"]),
        ("sid", settings["short_id"]),
        ("type", "tcp"),
    ]
    query = urlencode(params, safe="-_.~")
    fragment = quote(user["name"], safe="")
    return (
        f"vless://{user['id']}@{settings['xray_domain']}:{settings['xray_port']}"
        f"?{query}#{fragment}"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/scalar", status_code=307)


@app.get("/users", response_model=list[UserOut])
def list_users(_: None = Depends(_authorize)) -> list[dict[str, str]]:
    with state_lock:
        return _load_users()


@app.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, _: None = Depends(_authorize)) -> dict[str, str]:
    with state_lock:
        users = _load_users()
        if _find_user(users, payload.name):
            raise HTTPException(status_code=409, detail="User name already exists")

        user_id = payload.id or str(uuid.uuid4())
        try:
            _validate_uuid(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid UUID") from exc

        if any(u["id"] == user_id for u in users):
            raise HTTPException(status_code=409, detail="UUID already exists")

        protocol = _validate_protocol(payload.protocol)
        created: dict[str, str] = {"name": payload.name, "id": user_id, "protocol": protocol}
        if protocol == "vless":
            created["flow"] = payload.flow
        users.append(created)
        _assert_uniques(users)
        _save_users(users)
        _apply(users)
        return created


@app.patch("/users/{name}", response_model=UserOut)
def update_user(name: str, payload: UserUpdate, _: None = Depends(_authorize)) -> dict[str, str]:
    with state_lock:
        users = _load_users()
        user = _find_user(users, name)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if payload.new_name and payload.new_name != name and _find_user(users, payload.new_name):
            raise HTTPException(status_code=409, detail="Target user name already exists")

        if payload.id:
            try:
                _validate_uuid(payload.id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid UUID") from exc
            if any(u["id"] == payload.id and u["name"] != name for u in users):
                raise HTTPException(status_code=409, detail="UUID already exists")

        if payload.new_name:
            user["name"] = payload.new_name
        if payload.id:
            user["id"] = payload.id
        if payload.protocol:
            user["protocol"] = _validate_protocol(payload.protocol)
            if user["protocol"] == "vmess":
                user.pop("flow", None)
            else:
                user["flow"] = user.get("flow", DEFAULT_FLOW)
        if payload.flow:
            if user.get("protocol", DEFAULT_PROTOCOL) != "vless":
                raise HTTPException(status_code=400, detail="flow is only supported for protocol=vless")
            user["flow"] = payload.flow

        _assert_uniques(users)
        _save_users(users)
        _apply(users)
        return user


@app.delete("/users/{name}")
def delete_user(name: str, _: None = Depends(_authorize)) -> dict[str, str]:
    with state_lock:
        users = _load_users()
        user = _find_user(users, name)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        new_users = [u for u in users if u["name"] != name]
        if not new_users:
            raise HTTPException(status_code=400, detail="Cannot remove last user")

        _save_users(new_users)
        _apply(new_users)
        return {"status": "deleted", "name": name}


@app.get("/users/{name}/url", response_model=UrlOut)
def get_user_url(name: str, fp: str = DEFAULT_FP, _: None = Depends(_authorize)) -> dict[str, str]:
    with state_lock:
        users = _load_users()
        user = _find_user(users, name)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"name": user["name"], "url": _build_url(user, fp=fp)}


@app.get("/scalar", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title="Xray User API",
    )
