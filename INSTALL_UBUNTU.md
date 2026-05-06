# Install Ubuntu 22.04/24.04

These commands assume:

- control server code will live in `/opt/domain-drop-catcher`
- worker server code will live in `/opt/domain-drop-catcher`
- you already uploaded the repository files to the server before running the app setup section
- every line below is one command

## 1. Control Server

### 1.1 Base packages

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git nginx postgresql postgresql-contrib python3 python3-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

### 1.2 PostgreSQL

```bash
sudo -u postgres psql -c "CREATE USER dropcatcher WITH PASSWORD 'CHANGE_ME_DB_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE dropcatcher OWNER dropcatcher;"
```

### 1.3 Project directory

If the repository is already on the server in the current directory:

```bash
sudo mkdir -p /opt/domain-drop-catcher
sudo rsync -a ./ /opt/domain-drop-catcher/
sudo chown -R $USER:$USER /opt/domain-drop-catcher
```

If you use git instead, replace the `rsync` line with your own `git clone ... /opt/domain-drop-catcher`.

### 1.4 Backend install

```bash
cd /opt/domain-drop-catcher/backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .[dev]
```

### 1.5 Frontend install

```bash
cd /opt/domain-drop-catcher/frontend
npm install
npm run build
```

### 1.6 Control `.env`

```bash
cp /opt/domain-drop-catcher/backend/.env.example /opt/domain-drop-catcher/backend/.env
sed -i "s|^DB_URL=.*|DB_URL=postgresql+asyncpg://dropcatcher:CHANGE_ME_DB_PASSWORD@127.0.0.1:5432/dropcatcher|" /opt/domain-drop-catcher/backend/.env
sed -i "s|^SESSION_SECRET_KEY=.*|SESSION_SECRET_KEY=CHANGE_ME_LONG_RANDOM_SECRET|" /opt/domain-drop-catcher/backend/.env
sed -i "s|^OWNER_LOGIN=.*|OWNER_LOGIN=owner|" /opt/domain-drop-catcher/backend/.env
sed -i "s|^OWNER_PASSWORD=.*|OWNER_PASSWORD=CHANGE_ME_OWNER_PASSWORD|" /opt/domain-drop-catcher/backend/.env
sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=false|" /opt/domain-drop-catcher/backend/.env
sed -i "s|^CORS_ORIGINS=.*|CORS_ORIGINS=*|" /opt/domain-drop-catcher/backend/.env
```

### 1.7 systemd for control

```bash
sudo cp /opt/domain-drop-catcher/deploy/domain-drop-control.service /etc/systemd/system/domain-drop-control.service
sudo systemctl daemon-reload
sudo systemctl enable domain-drop-control.service
sudo systemctl start domain-drop-control.service
sudo systemctl status domain-drop-control.service --no-pager
```

### 1.8 Nginx for control

```bash
sudo cp /opt/domain-drop-catcher/deploy/nginx-domain-drop-catcher.conf /etc/nginx/sites-available/domain-drop-catcher
sudo ln -sf /etc/nginx/sites-available/domain-drop-catcher /etc/nginx/sites-enabled/domain-drop-catcher
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl status nginx --no-pager
```

### 1.9 HTTPS with Certbot

Replace `YOUR_DOMAIN` with your real domain:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo sed -i "s|server_name _;|server_name YOUR_DOMAIN;|" /etc/nginx/sites-available/domain-drop-catcher
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d YOUR_DOMAIN
sudo sed -i "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=true|" /opt/domain-drop-catcher/backend/.env
sudo systemctl restart domain-drop-control.service
```

## 2. Worker Server

### 2.1 Base packages

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3 python3-venv python3-pip
```

### 2.2 Project directory

If the repository is already on the server in the current directory:

```bash
sudo mkdir -p /opt/domain-drop-catcher
sudo rsync -a ./ /opt/domain-drop-catcher/
sudo chown -R $USER:$USER /opt/domain-drop-catcher
```

### 2.3 Worker install

```bash
cd /opt/domain-drop-catcher/worker
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

### 2.4 Worker `.env`

Replace placeholders with real values from the control panel:

```bash
cp /opt/domain-drop-catcher/worker/.env.example /opt/domain-drop-catcher/worker/.env
sed -i "s|^CONTROL_BASE_URL=.*|CONTROL_BASE_URL=https://YOUR_CONTROL_DOMAIN|" /opt/domain-drop-catcher/worker/.env
sed -i "s|^WORKER_ID=.*|WORKER_ID=CHANGE_ME_WORKER_ID|" /opt/domain-drop-catcher/worker/.env
sed -i "s|^CONTROL_TOKEN=.*|CONTROL_TOKEN=CHANGE_ME_WORKER_TOKEN|" /opt/domain-drop-catcher/worker/.env
sed -i "s|^SIMULATE_MODE=.*|SIMULATE_MODE=false|" /opt/domain-drop-catcher/worker/.env
```

### 2.5 systemd for worker

```bash
sudo cp /opt/domain-drop-catcher/deploy/domain-drop-worker.service /etc/systemd/system/domain-drop-worker.service
sudo systemctl daemon-reload
sudo systemctl enable domain-drop-worker.service
sudo systemctl start domain-drop-worker.service
sudo systemctl status domain-drop-worker.service --no-pager
```

## 3. Operational Commands

### 3.1 Control logs

```bash
journalctl -u domain-drop-control.service -f
```

### 3.2 Worker logs

```bash
journalctl -u domain-drop-worker.service -f
```

### 3.3 Restart control

```bash
sudo systemctl restart domain-drop-control.service
```

### 3.4 Restart worker

```bash
sudo systemctl restart domain-drop-worker.service
```

### 3.5 Rebuild frontend after update

```bash
cd /opt/domain-drop-catcher/frontend
npm install
npm run build
sudo systemctl restart domain-drop-control.service
```

### 3.6 Update backend after code change

```bash
cd /opt/domain-drop-catcher/backend
.venv/bin/pip install -e .[dev]
sudo systemctl restart domain-drop-control.service
```

### 3.7 Update worker after code change

```bash
cd /opt/domain-drop-catcher/worker
.venv/bin/pip install -e .
sudo systemctl restart domain-drop-worker.service
```

## 4. First Launch Checklist

After the services are running:

1. Open the control panel in the browser.
2. Log in with `OWNER_LOGIN` / `OWNER_PASSWORD`.
3. Create at least one contact profile.
4. Create a Gandi registrar account.
5. Validate the account from the panel.
6. Create one worker entry in the panel. If you leave `control_token` empty, control will auto-generate it for you.
7. Copy that worker `id` and `control_token` from the worker card into the worker `.env`.
8. Restart the worker service.
9. Add a test domain with `drop_date`.
10. Start or rebalance attacks from the panel.
