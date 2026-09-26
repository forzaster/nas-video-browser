#!/usr/bin/env bash
# LAN向け起動スクリプト。LAN側IPだけで待ち受け、トークン・ホスト名・接続元IPで制限する。
set -euo pipefail

usage() {
  cat <<'EOF'
使い方: ./launch.sh [-token TOKEN] [-name MAC名] [-allowip IP/CIDR,...] [-port PORT]

  -token    アクセストークン。省略時は環境変数 DLNA_TOKEN、それも無ければ入力を求めます
            （コマンドラインで渡すとシェル履歴に残るため、入力か環境変数がおすすめ）
  -name     Macのホスト名（例: mymac または mymac.local）。省略時はローカルホスト名
  -allowip  許可する接続元（例: '192.168.1.0/24,!192.168.1.1'）。! は除外。
            省略時はLANのサブネットを許可し、ルーター（デフォルトゲートウェイ）を除外
  -port     待ち受けポート（既定: 8765）
EOF
}

token="${DLNA_TOKEN:-}" name="" allowip="" port=8765
while [ $# -gt 0 ]; do
  case "$1" in
    -token) token="${2:?-token に値がありません}"; shift 2 ;;
    -name) name="${2:?-name に値がありません}"; shift 2 ;;
    -allowip) allowip="${2:?-allowip に値がありません}"; shift 2 ;;
    -port) port="${2:?-port に値がありません}"; shift 2 ;;
    -h|-help|--help) usage; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; usage >&2; exit 2 ;;
  esac
done

iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
lan_ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
if [ -z "$lan_ip" ]; then
  echo "LAN IPを取得できません（ネットワーク未接続、またはVPN経由の可能性）" >&2
  exit 1
fi

name="${name:-$(scutil --get LocalHostName)}"
case "$name" in *.*) ;; *) name="$name.local" ;; esac

if [ -z "$allowip" ]; then
  mask=$(ipconfig getoption "$iface" subnet_mask)
  gateway=$(route -n get default | awk '/gateway:/{print $2}')
  subnet=$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1] + "/" + sys.argv[2]).network)' "$lan_ip" "$mask")
  allowip="$subnet${gateway:+,!$gateway}"
fi

if [ -z "$token" ]; then
  read -rsp "アクセストークン: " token; echo
  [ -n "$token" ] || { echo "トークンが必要です" >&2; exit 1; }
fi

echo "インターフェース: $iface ($lan_ip)"
echo "許可ホスト名:     $lan_ip, $name"
echo "許可接続元:       $allowip"
echo "URL:              http://$name:$port"

cd "$(dirname "$0")"
DLNA_TOKEN="$token" \
DLNA_ALLOWED_HOSTS="$lan_ip,$name" \
DLNA_ALLOWED_CLIENTS="$allowip" \
DLNA_INTERFACE="$lan_ip" \
exec uv run uvicorn app.main:create_app --factory --host "$lan_ip" --port "$port"
