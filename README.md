# Xray VLESS deploy (Ansible + Docker)

## 1) Prepare local config files

Copy example files:

```bash
cp ansible/inventory/hosts.example.yml ansible/inventory/hosts.yml
cp ansible/inventory/group_vars/all.example.yml ansible/inventory/group_vars/all.yml
cp ansible/inventory/group_vars/users.example.json ansible/inventory/group_vars/users.json
```

Then edit:

- `ansible/inventory/hosts.yml` -> set VPS IP/user/password
- `ansible/inventory/group_vars/all.yml` -> set:
  - `xray_domain`
  - `reality_private_key`
  - `reality_public_key`
  - `short_id`
  - `xray_api_port`
  - `xray_api_token` (required for API auth)

Generate values:

- UUID: `uuidgen`
- Reality keys:
  - `docker run --rm teddysun/xray xray x25519`
  - or local xray: `xray x25519`

## 2) Manage users locally (inventory source)

Source-of-truth for initial deploy:

- `ansible/inventory/group_vars/users.json`

Helper script:

```bash
python scripts/vless_users.py list
python scripts/vless_users.py add alice
python scripts/vless_users.py update alice --new-name alice-phone
python scripts/vless_users.py remove alice-phone
python scripts/vless_users.py url alice
```

## 3) Deploy

```bash
cd ansible
ansible-playbook playbooks/deploy_xray.yml
```

This deploy starts two containers:

- `xray` (VLESS server)
- `xray-api` (REST API for user management)

## 4) API + docs (Scalar-like)

Quick API summary:

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

After deploy:

- Swagger docs: `http://SERVER_IP:8080/docs`
- Scalar docs: `http://SERVER_IP:8080/scalar`
- OpenAPI: `http://SERVER_IP:8080/openapi.json`

Replace `8080` with `xray_api_port` from `all.yml`.

### API examples

List users:

```bash
curl http://SERVER_IP:8080/users \
  -H 'X-API-Key: YOUR_TOKEN'
```

Create user (UUID auto-generated if `id` omitted):

```bash
curl -X POST http://SERVER_IP:8080/users \
  -H 'X-API-Key: YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"name":"alice"}'
```

Update user:

```bash
curl -X PATCH http://SERVER_IP:8080/users/alice \
  -H 'X-API-Key: YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"new_name":"alice-phone"}'
```

Delete user:

```bash
curl -X DELETE http://SERVER_IP:8080/users/alice-phone \
  -H 'X-API-Key: YOUR_TOKEN'
```

Get VLESS URL:

```bash
curl http://SERVER_IP:8080/users/default/url \
  -H 'X-API-Key: YOUR_TOKEN'
```

Every create/update/delete call updates `/opt/xray/config.json` and restarts container `xray` automatically.

## 5) Safe GitHub publishing

Sensitive files are intentionally ignored by `.gitignore`:

- `ansible/inventory/hosts.yml`
- `ansible/inventory/group_vars/all.yml`
- `ansible/inventory/group_vars/users.json`
- `secrets/vault.yml`

Publish flow:

```bash
git init
git add .
git status
git commit -m "Initial public-safe commit"
git remote add origin git@github.com:YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Before push, verify no secrets staged:

```bash
git status
git diff --cached
```
