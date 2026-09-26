#!/usr/bin/env bash
# launch.sh をログイン時に自動起動する LaunchAgent（launchd）として登録・管理する。
set -euo pipefail

usage() {
  cat <<'EOF'
使い方:
  ./install-daemon.sh [install] [-name MAC名] [-allowip IP/CIDR,...] [-port PORT] [-data DIR] [-dry-run]
  ./install-daemon.sh uninstall | status | restart | logs

  install    LaunchAgentを作成・登録して起動（既に登録済みなら置き換え）
             管理者/ゲストトークンは入力を求めます（環境変数 DLNA_ADMIN_TOKEN / DLNA_GUEST_TOKEN があればそれを使用）
    -name    Macのホスト名（launch.sh -name に渡す）
    -allowip 許可する接続元（launch.sh -allowip に渡す。例: '192.168.1.0/24,!192.168.1.1'）
    -port    待ち受けポート（既定: 8765）
    -data    SQLite・サムネイルの保存先（既定: ~/Library/Application Support/webvideo）
             保存先が空で ./data にデータがあればコピーして引き継ぎます
    -dry-run 登録せず、生成する設定（トークンは伏せ字）を表示
  uninstall  停止して登録と設定ファイルを削除（データは残します）
  status     実行状態を表示
  restart    再起動（コード更新後など）
  logs       ログを追跡表示
EOF
}

LABEL="local.webvideo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/webvideo.log"
DOMAIN="gui/$(id -u)"
ROOT="$(cd "$(dirname "$0")" && pwd)"

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

command="install"
case "${1:-}" in install|uninstall|status|restart|logs) command="$1"; shift ;; -h|-help|--help) usage; exit 0 ;; esac

case "$command" in
  uninstall)
    loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"
    echo "登録を解除しました（データとログは残しています）"; exit 0 ;;
  status)
    loaded || { echo "未登録です"; exit 1; }
    launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|pid|last exit code|runs) =' || true; exit 0 ;;
  restart)
    loaded || { echo "未登録です。先に ./install-daemon.sh を実行してください" >&2; exit 1; }
    launchctl kickstart -k "$DOMAIN/$LABEL"; echo "再起動しました"; exit 0 ;;
  logs)
    exec tail -n 50 -f "$LOG" ;;
esac

args=() port=8765 data="$HOME/Library/Application Support/webvideo" dry=false
while [ $# -gt 0 ]; do
  case "$1" in
    -name|-allowip) args+=("$1" "${2:?$1 に値がありません}"); shift 2 ;;
    -port) port="${2:?-port に値がありません}"; shift 2 ;;
    -data) data="${2:?-data に値がありません}"; shift 2 ;;
    -dry-run) dry=true; shift ;;
    -h|-help|--help) usage; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; usage >&2; exit 2 ;;
  esac
done
args+=(-port "$port")
case "$ROOT" in */.claude/worktrees/*)
  echo "注意: 作業用worktree（$ROOT）から実行しています。常用する場合はメインのチェックアウトで実行してください" >&2 ;;
esac

# launchd の PATH はほぼ空なので、uv と ffmpeg の場所を明示する
uv_path=$(command -v uv) || { echo "uv が見つかりません" >&2; exit 1; }
path_dirs="$(dirname "$uv_path")"
if ffmpeg_path=$(command -v ffmpeg); then
  path_dirs="$path_dirs:$(dirname "$ffmpeg_path")"
else
  echo "注意: ffmpeg が見つかりません。サムネイルは作成されません" >&2
fi
path_dirs="$path_dirs:/usr/bin:/bin:/usr/sbin:/sbin"

admin="${DLNA_ADMIN_TOKEN:-${DLNA_TOKEN:-}}" guest="${DLNA_GUEST_TOKEN:-}"
if $dry; then
  admin="${admin:-dry-run}"
else
  if [ -z "$admin" ]; then
    read -rsp "管理者トークン: " admin; echo
    [ -n "$admin" ] || { echo "管理者トークンが必要です" >&2; exit 1; }
  fi
  if [ -z "${DLNA_GUEST_TOKEN+x}" ]; then
    read -rsp "ゲストトークン（空欄でゲストなし）: " guest; echo
  fi
  if [ -n "$guest" ] && [ "$guest" = "$admin" ]; then
    echo "ゲストトークンには管理者トークンと別の値を指定してください" >&2; exit 1
  fi
fi

# plistlib で書き出す（XMLエスケープのため）。トークンは引数でなく環境変数で渡し ps に出さない
write_plist() {
  (umask 077
   PL_OUT="$1" PL_LABEL="$LABEL" PL_ROOT="$ROOT" PL_PATH="$path_dirs" PL_DATA="$data" PL_LOG="$LOG" \
   PL_ADMIN="$2" PL_GUEST="$3" python3 - "${args[@]}" <<'EOF'
import os, plistlib, sys
e = os.environ
env = {"PATH": e["PL_PATH"], "DLNA_DATA": e["PL_DATA"], "DLNA_ADMIN_TOKEN": e["PL_ADMIN"]}
if e["PL_GUEST"]:
    env["DLNA_GUEST_TOKEN"] = e["PL_GUEST"]
with open(e["PL_OUT"], "wb") as f:
    plistlib.dump({"Label": e["PL_LABEL"], "ProgramArguments": [e["PL_ROOT"] + "/launch.sh", *sys.argv[1:]],
                   "WorkingDirectory": e["PL_ROOT"], "EnvironmentVariables": env,
                   "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                   "StandardOutPath": e["PL_LOG"], "StandardErrorPath": e["PL_LOG"]}, f)
EOF
  )
  plutil -lint -s "$1"
}

if $dry; then
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  write_plist "$tmp" "********" "$([ -n "$guest" ] && echo "********")"
  echo "== $PLIST（登録はしていません）"
  cat "$tmp"
  exit 0
fi

if [ ! -e "$data/catalog.sqlite3" ] && [ -e "$ROOT/data/catalog.sqlite3" ]; then
  mkdir -p "$data"
  cp -Rn "$ROOT/data/." "$data/"
  echo "既存データを引き継ぎました: $ROOT/data -> $data"
fi
mkdir -p "$data" "$(dirname "$PLIST")" "$(dirname "$LOG")"

if ! loaded && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "注意: ポート $port は既に使われています。手動で起動したサーバーを止めてから再実行してください" >&2
  exit 1
fi

loaded && launchctl bootout "$DOMAIN/$LABEL" && sleep 1
write_plist "$PLIST" "$admin" "$guest"
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "登録して起動しました: $LABEL"
echo "  設定: $PLIST（本人のみ読み書き可）"
echo "  データ: $data"
echo "  ログ: $LOG"
echo "初回はローカルネットワークへのアクセス許可を求められることがあります。「許可」を選んでください。"
echo "状態確認: ./install-daemon.sh status / 停止・解除: ./install-daemon.sh uninstall"
