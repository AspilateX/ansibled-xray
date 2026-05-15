# Xray deploy (Ansible + Docker)

## 1) Install Ansible
```bash
sudo apt update
sudo apt install -y ansible
```

## 2) Prepare local config files

Copy example files:

```bash
cp ansible/inventory/group_vars/all.example.yml ansible/inventory/group_vars/all.yml
cp secrets/vault.example.yml secrets/vault.yml
```

Then edit:

- `ansible/inventory/hosts.yml` -> committed in repo, default is local execution on this VPS
- `ansible/inventory/group_vars/all.yml` -> set:
  - `xray_domain`
  - `xray_api_port`
  - `xray_vmess_port` (port for VMess users)
  - `xray_manage_nginx` (`true` by default; set `false` to skip nginx automation)
- `secrets/vault.yml` -> set:
  - `reality_private_key`
  - `reality_public_key`
  - `short_id`
  - `xray_api_token` (required for API auth)

Optional: encrypt secrets file with Ansible Vault:

```bash
ansible-vault encrypt secrets/vault.yml
```

Generate values:

- UUID: `uuidgen`
- Reality keys:
  - `docker run --rm teddysun/xray xray x25519`
  - or local xray: `xray x25519`

## 3) Manage users locally

Source-of-truth for initial deploy:

- `ansible/inventory/group_vars/users.json`
- If this file is missing, Ansible auto-creates it from `ansible/inventory/group_vars/users.example.json` on deploy

Helper script:

```bash
python scripts/xray_users.py list
python scripts/xray_users.py add alice
python scripts/xray_users.py add bob --protocol vmess
python scripts/xray_users.py update alice --new-name alice-phone
python scripts/xray_users.py update bob --protocol vless --flow xtls-rprx-vision
python scripts/xray_users.py remove alice-phone
python scripts/xray_users.py url alice
python scripts/xray_users.py url bob
```

## 4) Deploy

```bash
cd ansible
ansible-playbook playbooks/deploy_xray.yml
```

If `secrets/vault.yml` is encrypted:

```bash
cd ansible
ansible-playbook playbooks/deploy_xray.yml --ask-vault-pass
```

This deploy starts two containers:

- `xray` (Xray server for VLESS/VMess users)
- `xray-api` (REST API for user management)

During deploy Ansible also configures nginx:

- If nginx is missing, it gets installed automatically
- A reverse-proxy site is created for `xray_domain` on port `80`
- API becomes available by domain without API port in URL:
  - `http://xray_domain/docs`
  - `http://xray_domain/scalar`
  - `http://xray_domain/openapi.json`
- To disable this behavior, set `xray_manage_nginx: false` in `all.yml`

Note: this setup does not bind nginx to `443`, because `443` is used by Xray.

## 5) API

API summary:

- Base URL: `http://SERVER_IP:xray_api_port/`
- Root `/` redirects to `/scalar`
- Auth: header `X-API-Key: <xray_api_token>`
- Main endpoints:
  - `GET /users`
  - `POST /users`
  - `PATCH /users/{name}`
  - `DELETE /users/{name}`
  - `GET /users/{name}/url`
- Any create/update/delete automatically rewrites Xray config and restarts `xray`
- User object fields:
  - `name`: string
  - `id`: UUID
  - `protocol`: `vless` or `vmess` (optional, default `vless`)
  - `flow`: only for `vless` (default `xtls-rprx-vision`)

After deploy:

- By domain through nginx:
  - Swagger docs: `http://xray_domain/docs`
  - Scalar docs: `http://xray_domain/scalar`
  - OpenAPI: `http://xray_domain/openapi.json`
- Direct API port (debug/troubleshooting): `http://SERVER_IP:xray_api_port/`
