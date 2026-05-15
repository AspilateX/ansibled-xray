#!/usr/bin/env python3
"""Manage Xray users for this Ansible + Xray project."""

from __future__ import annotations

import base64
import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

DEFAULT_FLOW = "xtls-rprx-vision"
DEFAULT_FP = "chrome"
DEFAULT_PROTOCOL = "vless"
SUPPORTED_PROTOCOLS = {"vless", "vmess"}
DEFAULT_VMESS_PORT = "10086"


class ConfigError(RuntimeError):
    pass


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def users_file_path() -> Path:
    return project_root() / "ansible" / "inventory" / "group_vars" / "users.json"


def settings_file_path() -> Path:
    return project_root() / "ansible" / "inventory" / "group_vars" / "all.yml"


def vault_file_path() -> Path:
    return project_root() / "secrets" / "vault.yml"


def load_users(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise ConfigError(f"{path} must contain a JSON array")

    users: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("Each user must be a JSON object")
        name = item.get("name")
        user_id = item.get("id")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("Each user must include non-empty 'name'")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ConfigError("Each user must include non-empty 'id'")
        validate_uuid(user_id)

        protocol = validate_protocol(str(item.get("protocol", DEFAULT_PROTOCOL)))
        user: dict[str, Any] = {
            "name": name.strip(),
            "id": user_id.strip(),
            "protocol": protocol,
        }
        if protocol == "vless":
            flow = item.get("flow", DEFAULT_FLOW)
            if not isinstance(flow, str) or not flow.strip():
                raise ConfigError("User 'flow' must be a non-empty string for protocol=vless")
            user["flow"] = flow.strip()
        users.append(user)

    ensure_uniques(users)
    return users


def save_users(path: Path, users: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def validate_uuid(value: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid UUID: {value}") from exc


def validate_protocol(value: str) -> str:
    protocol = value.strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ConfigError(
            f"Unsupported protocol '{value}'. Supported: {', '.join(sorted(SUPPORTED_PROTOCOLS))}"
        )
    return protocol


def ensure_uniques(users: list[dict[str, Any]]) -> None:
    names = [u["name"] for u in users]
    ids = [u["id"] for u in users]
    if len(names) != len(set(names)):
        raise ConfigError("User names must be unique")
    if len(ids) != len(set(ids)):
        raise ConfigError("User IDs (UUID) must be unique")


def find_user(users: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((u for u in users if u["name"] == name), None)


def parse_key_value_yml(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")

    content = path.read_text(encoding="utf-8")
    if content.startswith("$ANSIBLE_VAULT;"):
        raise ConfigError(
            f"Cannot parse encrypted vault file directly: {path}. Use API endpoint for URL generation or decrypt vault first."
        )

    result: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue

        key, raw_value = match.groups()
        value = raw_value.split("#", 1)[0].strip()
        if not value:
            continue

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value

    return result


def load_settings(all_path: Path, vault_path: Path) -> dict[str, str]:
    settings = parse_key_value_yml(all_path)
    if vault_path.exists():
        settings.update(parse_key_value_yml(vault_path))
    settings.setdefault("xray_vmess_port", DEFAULT_VMESS_PORT)
    return settings


def build_vless_url(user: dict[str, Any], settings: dict[str, str], fp: str) -> str:
    required = ["xray_domain", "xray_port", "reality_public_key", "short_id"]
    missing = [k for k in required if not settings.get(k)]
    if missing:
        raise ConfigError(
            "Missing keys in all.yml for URL generation: " + ", ".join(sorted(missing))
        )

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


def build_vmess_url(user: dict[str, Any], settings: dict[str, str]) -> str:
    required = ["xray_domain", "xray_vmess_port"]
    missing = [k for k in required if not settings.get(k)]
    if missing:
        raise ConfigError(
            "Missing keys in settings for VMess URL generation: " + ", ".join(sorted(missing))
        )

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
    encoded = base64.b64encode(
        json.dumps(vmess_payload, ensure_ascii=True).encode("utf-8")
    ).decode("ascii")
    return f"vmess://{encoded}"


def build_user_url(user: dict[str, Any], settings: dict[str, str], fp: str) -> str:
    protocol = user.get("protocol", DEFAULT_PROTOCOL)
    if protocol == "vmess":
        return build_vmess_url(user, settings)
    return build_vless_url(user, settings, fp)


def cmd_list(args: argparse.Namespace) -> int:
    users = load_users(args.users_file)
    if not users:
        print("No users found")
        return 0

    width = max(len(u["name"]) for u in users)
    header = f"{'NAME'.ljust(width)}  UUID                                  PROTOCOL  FLOW"
    print(header)
    for user in users:
        flow = user.get("flow", "-")
        print(
            f"{user['name'].ljust(width)}  {user['id']}  {user['protocol'].ljust(8)}  {flow}"
        )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    users = load_users(args.users_file)
    if find_user(users, args.name):
        raise ConfigError(f"User already exists: {args.name}")

    user_id = args.user_id or str(uuid.uuid4())
    validate_uuid(user_id)

    if any(u["id"] == user_id for u in users):
        raise ConfigError(f"UUID already in use: {user_id}")

    protocol = validate_protocol(args.protocol)
    created: dict[str, Any] = {
        "name": args.name,
        "id": user_id,
        "protocol": protocol,
    }
    if protocol == "vless":
        created["flow"] = args.flow
    users.append(created)
    ensure_uniques(users)
    save_users(args.users_file, users)
    print(f"Created user '{args.name}' with id {user_id}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    users = load_users(args.users_file)
    user = find_user(users, args.name)
    if not user:
        raise ConfigError(f"User not found: {args.name}")

    new_users = [u for u in users if u["name"] != args.name]
    if not new_users:
        raise ConfigError("Cannot remove the last user")

    save_users(args.users_file, new_users)
    print(f"Removed user '{args.name}'")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    users = load_users(args.users_file)
    user = find_user(users, args.name)
    if not user:
        raise ConfigError(f"User not found: {args.name}")

    if args.new_name and args.new_name != args.name and find_user(users, args.new_name):
        raise ConfigError(f"User with name '{args.new_name}' already exists")

    if args.user_id:
        validate_uuid(args.user_id)
        if any(u["id"] == args.user_id and u["name"] != args.name for u in users):
            raise ConfigError(f"UUID already in use: {args.user_id}")

    if args.new_name:
        user["name"] = args.new_name
    if args.user_id:
        user["id"] = args.user_id
    if args.protocol:
        user["protocol"] = validate_protocol(args.protocol)
        if user["protocol"] == "vmess":
            user.pop("flow", None)
        else:
            user["flow"] = user.get("flow", DEFAULT_FLOW)
    if args.flow:
        if user.get("protocol", DEFAULT_PROTOCOL) != "vless":
            raise ConfigError("flow is only supported for protocol=vless")
        user["flow"] = args.flow

    ensure_uniques(users)
    save_users(args.users_file, users)
    print(f"Updated user '{args.name}'")
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    users = load_users(args.users_file)
    user = find_user(users, args.name)
    if not user:
        raise ConfigError(f"User not found: {args.name}")

    settings = load_settings(args.settings_file, args.vault_file)
    print(build_user_url(user, settings, args.fp))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage VLESS users")
    parser.add_argument(
        "--users-file",
        type=Path,
        default=users_file_path(),
        help="Path to users.json",
    )
    parser.add_argument(
        "--settings-file",
        type=Path,
        default=settings_file_path(),
        help="Path to all.yml",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="List users")
    p_list.set_defaults(func=cmd_list)

    p_add = subparsers.add_parser("add", help="Create user")
    p_add.add_argument("name", help="User name")
    p_add.add_argument("--id", dest="user_id", help="UUID (auto-generated if omitted)")
    p_add.add_argument("--flow", default=DEFAULT_FLOW, help="VLESS flow")
    p_add.set_defaults(func=cmd_add)

    p_remove = subparsers.add_parser("remove", help="Delete user")
    p_remove.add_argument("name", help="User name")
    p_remove.set_defaults(func=cmd_remove)

    p_update = subparsers.add_parser("update", help="Edit user")
    p_update.add_argument("name", help="Current user name")
    p_update.add_argument("--new-name", help="New user name")
    p_update.add_argument("--id", dest="user_id", help="New UUID")
    p_update.add_argument("--flow", help="New flow")
    p_update.set_defaults(func=cmd_update)

    p_url = subparsers.add_parser("url", help="Generate VLESS URL")
    p_url.add_argument("name", help="User name")
    p_url.add_argument("--fp", default=DEFAULT_FP, help="Client fingerprint")
    p_url.set_defaults(func=cmd_url)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
