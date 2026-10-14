#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y nginx ufw openjdk-21-jre-headless
cat >/etc/nginx/sites-available/editorial <<'NGINX'
server {
  listen 10.20.30.45:4007;
  server_name _;
  client_max_body_size 100m;
  location / {
    proxy_pass http://127.0.0.1:14007;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 180s;
  }
}
server {
  listen 10.20.30.45:8090;
  server_name _;
  client_max_body_size 100m;
  location /api/max-bridge/ { return 403; }
  location / {
    proxy_pass http://127.0.0.1:8091;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300s;
  }
}
NGINX
ln -sf /etc/nginx/sites-available/editorial /etc/nginx/sites-enabled/editorial
nginx -t
install -d /etc/systemd/system/nginx.service.d
install -m 0644 /opt/max-postiz/deployment/linux/nginx-network.conf /etc/systemd/system/nginx.service.d/network.conf
systemctl daemon-reload
systemctl reload nginx
ufw default deny incoming
ufw default allow outgoing
ufw allow from 10.20.30.0/24 to any port 22 proto tcp
ufw allow from 10.20.30.0/24 to any port 4007 proto tcp
ufw allow from 10.20.30.0/24 to any port 8090 proto tcp
ufw allow from 172.16.0.0/12 to any port 8091 proto tcp
# Owner already reaches this LAN over WireGuard; no additional VPN account.
OWNER_VPN_CIDR=${OWNER_VPN_CIDR:-10.77.0.2/32}
ufw allow from "$OWNER_VPN_CIDR" to any port 22 proto tcp
ufw allow from "$OWNER_VPN_CIDR" to any port 4007 proto tcp
ufw allow from "$OWNER_VPN_CIDR" to any port 8090 proto tcp
ufw --force enable
install -d -m 0755 /opt/languagetool
curl -fsSL https://languagetool.org/download/LanguageTool-stable.zip -o /var/cache/editorial/LanguageTool-stable.zip
sha256sum /var/cache/editorial/LanguageTool-stable.zip >/var/cache/editorial/LanguageTool.sha256
unzip -qo /var/cache/editorial/LanguageTool-stable.zip -d /opt/languagetool
directory=$(find /opt/languagetool -mindepth 1 -maxdepth 1 -type d -name 'LanguageTool-*' | sort | tail -1)
test -n "$directory"
ln -sfn "$directory" /opt/languagetool/current
install -m 0644 /opt/max-postiz/deployment/linux/max-postiz-spelling.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now max-postiz-spelling
