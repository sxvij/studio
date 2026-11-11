#!/usr/bin/env bash
# Deploy the studio to CT 114 from this checkout (run on CT 200). Idempotent: also does the first-time setup.
# Steps: snapshot, stage and test in the container, install (user, dirs, Python venv with onnxruntime, cut-out model,
# firewall, units, nginx), restart, check that the gate refuses Cloudflare requests without a token.
# The Centre AI token is not in the repo: the first time, pass it on stdin with ./deploy.sh <reason> --token < file
# (it lands in /etc/studio/env, root:studio 0640).
# Usage: ./deploy.sh <snapshot-reason> [--token]
set -euo pipefail
CT=114
HOST=${PVE_HOST_ALIAS:-pve}
reason=${1:?usage: ./deploy.sh <snapshot-reason> [--token]}
[[ $reason =~ ^[a-z0-9-]+$ ]] || { echo "snapshot reason: lowercase letters, digits and dashes only" >&2; exit 1; }
cd "$(dirname "$0")"
if [[ -n $(git status --porcelain) ]]; then echo "Commit your changes first." >&2; exit 1; fi
rev=$(git rev-parse --short HEAD)

if [[ ${2:-} == --token ]]; then
  echo "== Centre AI token"
  grep -E '^AI_TOKEN=cai_[A-Za-z0-9_-]+$' | head -1 | ssh "$HOST" "pct exec $CT -- bash -c 'install -d -m 0755 /etc/studio && t=\$(cat) && test -n \"\$t\" && printf \"AI_URL=http://192.168.1.193:11500\n%s\n\" \"\$t\" > /etc/studio/env.new && mv /etc/studio/env.new /etc/studio/env && echo token saved'"
fi

echo "== snapshot claude-$(date +%Y%m%d)-$reason"
ssh "$HOST" pct snapshot "$CT" "claude-$(date +%Y%m%d)-$reason" >/dev/null

echo "== stage and test $rev"
git archive --format=tar HEAD app www server tests | ssh "$HOST" "pct exec $CT -- bash -c 'rm -rf /root/studio-stage && mkdir /root/studio-stage && tar -C /root/studio-stage -xf - && cd /root/studio-stage && python3 -B server/test_access_gate.py 2>&1 | tail -1 && python3 -B -W ignore tests/test_studio.py 2>&1 | tail -3'"

echo "== install"
ssh "$HOST" "pct exec $CT -- bash -s" <<EOS
set -e
S=/root/studio-stage
id studio >/dev/null 2>&1 || useradd --system --home-dir /var/lib/studio --shell /usr/sbin/nologin studio
install -d -o studio -g studio -m 0750 /var/lib/studio /var/lib/studio/tmp /var/lib/studio/models
install -d -m 0755 /opt/studio /opt/studio/gate /etc/studio
if [ ! -x /opt/studio/venv/bin/python ]; then
  python3 -m venv --system-site-packages /opt/studio/venv
  /opt/studio/venv/bin/pip install -q onnxruntime numpy
fi
M=/var/lib/studio/models/isnet-general-use.onnx
if [ ! -f \$M ]; then
  curl -sSfL --retry 3 -o \$M.part https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx
  echo "fc16ebd8b0c10d971d3513d564d01e29  \$M.part" | md5sum -c --quiet && mv \$M.part \$M && chown studio:studio \$M
fi
rm -rf /opt/studio/app.new /opt/studio/www.new
cp -r \$S/app /opt/studio/app.new && cp -r \$S/www /opt/studio/www.new
rm -rf /opt/studio/app.old /opt/studio/www.old
[ -d /opt/studio/app ] && mv /opt/studio/app /opt/studio/app.old; [ -d /opt/studio/www ] && mv /opt/studio/www /opt/studio/www.old
mv /opt/studio/app.new /opt/studio/app && mv /opt/studio/www.new /opt/studio/www
chmod -R a+rX /opt/studio/app /opt/studio/www
install -m 644 \$S/server/access_gate.py /opt/studio/gate/access_gate.py
echo "$rev" > /opt/studio/REVISION
[ -f /etc/studio/gate.env ] || printf 'CF_ACCESS_TEAM=sxvij\nCF_ACCESS_AUD=\n' > /etc/studio/gate.env
[ -f /etc/studio/env ] || printf 'AI_URL=http://192.168.1.193:11500\nAI_TOKEN=\n' > /etc/studio/env
chown root:studio /etc/studio/env && chmod 0640 /etc/studio/env
if ! cmp -s \$S/server/nftables.conf /etc/nftables.conf; then
  nft -c -f \$S/server/nftables.conf && cp -p /etc/nftables.conf /etc/nftables.conf.bak-\$(date +%Y%m%d%H%M) 2>/dev/null || true
  install -m 644 \$S/server/nftables.conf /etc/nftables.conf && systemctl enable --now nftables >/dev/null 2>&1 && nft -f /etc/nftables.conf
fi
install -m 644 \$S/server/studio.service \$S/server/studio-gate.service \$S/server/cloudflared-studio.service /etc/systemd/system/
install -m 644 \$S/server/nginx.conf /etc/nginx/sites-available/studio
ln -sf ../sites-available/studio /etc/nginx/sites-enabled/studio; rm -f /etc/nginx/sites-enabled/default
systemctl daemon-reload
systemctl enable --now studio-gate studio >/dev/null 2>&1; systemctl restart studio-gate studio
nginx -t 2>&1 | tail -1 && systemctl enable nginx >/dev/null 2>&1 && systemctl restart nginx
if [ -s /etc/cloudflared/studio.env ] && command -v cloudflared >/dev/null; then systemctl enable --now cloudflared-studio >/dev/null 2>&1; else echo "tunnel token not set yet (/etc/cloudflared/studio.env)"; fi
grep -q '^AI_TOKEN=cai_' /etc/studio/env || echo "WARNING: no Centre AI token in /etc/studio/env (deploy once with --token)"
sleep 2
systemctl is-active studio studio-gate nginx | tr '\n' ' '; echo
curl -s -o /dev/null -w "page (local) %{http_code}\n" http://127.0.0.1:8120/
curl -s -o /dev/null -w "api (local) %{http_code}\n" http://127.0.0.1:8120/api/config
curl -s -o /dev/null -w "through Cloudflare without a token %{http_code} (want 403)\n" -H "cf-ray: x" http://127.0.0.1:8120/
curl -s -o /dev/null -w "api through Cloudflare without a token %{http_code} (want 403)\n" -H "cf-ray: x" http://127.0.0.1:8120/api/items
curl -s -o /dev/null -w "write without X-Studio %{http_code} (want 400)\n" -X POST -H "Content-Type: application/json" -d '{}' http://127.0.0.1:8120/api/generate
rm -rf \$S
EOS
echo "== deployed $rev"
