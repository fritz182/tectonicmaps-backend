#!/bin/bash
# TectonicMaps Backend — Hetzner VPS Setup Script
# Run as root on a fresh Ubuntu 22.04+ server
set -e

APP_DIR="/opt/tectonicmaps"
DOMAIN="api.tectonicmaps.com"

echo "=== Installing system dependencies ==="
apt update && apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx gdal-bin libgdal-dev

echo "=== Creating app directory ==="
mkdir -p $APP_DIR
cd $APP_DIR

echo "=== Cloning route2tile ==="
if [ ! -d "$APP_DIR/route2tile" ]; then
    git clone https://github.com/fritz182/route2tile.git
else
    cd $APP_DIR/route2tile && git pull && cd $APP_DIR
fi

echo "=== Cloning backend ==="
if [ ! -d "$APP_DIR/backend" ]; then
    git clone https://github.com/fritz182/tectonicmaps-backend.git backend
else
    cd $APP_DIR/backend && git pull && cd $APP_DIR
fi

echo "=== Setting up Python venv ==="
python3 -m venv $APP_DIR/venv
source $APP_DIR/venv/bin/activate
pip install --upgrade pip
pip install -r $APP_DIR/backend/requirements.txt
pip install -e $APP_DIR/route2tile

echo "=== Creating systemd service ==="
cat > /etc/systemd/system/tectonicmaps.service << 'UNIT'
[Unit]
Description=TectonicMaps API
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/tectonicmaps/backend
Environment="ROUTE2TILE_DIR=/opt/tectonicmaps/route2tile"
Environment="ROUTE2TILE_BIN=/opt/tectonicmaps/venv/bin/route2tile"
ExecStart=/opt/tectonicmaps/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "=== Setting permissions ==="
chown -R www-data:www-data $APP_DIR

echo "=== Creating nginx config ==="
cat > /etc/nginx/sites-available/tectonicmaps-api << NGINX
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/tectonicmaps-api /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "=== Starting service ==="
systemctl daemon-reload
systemctl enable tectonicmaps
systemctl start tectonicmaps

echo ""
echo "=== Done! ==="
echo "API running at http://$DOMAIN"
echo ""
echo "Next steps:"
echo "  1. Point $DOMAIN A record to this server's IP (in Cloudflare DNS, proxy OFF)"
echo "  2. Run: certbot --nginx -d $DOMAIN"
echo "  3. Test: curl https://$DOMAIN/api/health"
