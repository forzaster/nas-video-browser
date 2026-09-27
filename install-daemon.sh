#!/usr/bin/env bash
# launch.sh をログイン時に自動起動する LaunchAgent（launchd）として登録・管理する。
# 起動は FamilyVideos.app（launcher/ からビルドし ad-hoc 署名する起動用アプリ）経由。launch.sh とその子プロセス（uv・python）は
# このアプリの責任で動くので、ローカルネットワークの許可とバックグラウンド項目は「FamilyVideos」として表示される。
set -euo pipefail

usage() {
  cat <<'EOF'
使い方:
  ./install-daemon.sh [install] [-name MAC名] [-allowip IP/CIDR,...] [-port PORT] [-data DIR] [-y] [-dry-run]
  ./install-daemon.sh uninstall | status | restart | logs

  install    LaunchAgentを作成・登録して起動（既に登録済みなら置き換え）
             未指定の項目は対話で聞きます（Enterで既定値）。最後に内容を確認してから登録します
             管理者/ゲストトークンも入力を求めます（環境変数 DLNA_ADMIN_TOKEN / DLNA_GUEST_TOKEN があればそれを使用）
    -name    Macのホスト名（launch.sh -name に渡す）
    -allowip 許可する接続元（launch.sh -allowip に渡す。例: '192.168.1.0/24,!192.168.1.1'）
    -port    待ち受けポート（既定: 8765）
    -data    SQLite・サムネイルの保存先（既定: ~/Library/Application Support/webvideo）
             保存先が空で ./data にデータがあればコピーして引き継ぎます
    -y       質問せず既定値で登録（管理者トークンは環境変数 DLNA_ADMIN_TOKEN が必要）
    -dry-run 登録せず、生成する設定（トークンは伏せ字）を表示
  uninstall  停止して登録・設定ファイル・起動用アプリを削除（データは残します）
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
# 起動用アプリはリポジトリの外に置く（チェックアウトを移動・作り直しても同じアプリのままで、許可を保てる）
APP="$HOME/Library/Application Support/FamilyVideos/FamilyVideos.app"
LAUNCHER="$APP/Contents/MacOS/FamilyVideos"

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

command="install"
case "${1:-}" in install|uninstall|status|restart|logs) command="$1"; shift ;; -h|-help|--help) usage; exit 0 ;; esac

case "$command" in
  uninstall)
    loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"
    rm -rf "$APP"
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

name="" allowip="" port="" data="" dry=false yes=false
while [ $# -gt 0 ]; do
  case "$1" in
    -name) name="${2:?-name に値がありません}"; shift 2 ;;
    -allowip) allowip="${2:?-allowip に値がありません}"; shift 2 ;;
    -port) port="${2:?-port に値がありません}"; shift 2 ;;
    -data) data="${2:?-data に値がありません}"; shift 2 ;;
    -y) yes=true; shift ;;
    -dry-run) dry=true; shift ;;
    -h|-help|--help) usage; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$ROOT" in */.claude/worktrees/*)
  echo "注意: 作業用worktree（${ROOT}）から実行しています。常用する場合はメインのチェックアウトで実行してください" >&2 ;;
esac
# 端末から実行し -y でないときだけ対話で聞く（それ以外は既定値）
interactive=false
[ -t 0 ] && ! $yes && interactive=true

# launchd の PATH はほぼ空なので、uv と ffmpeg の場所を明示する
uv_path=$(command -v uv) || { echo "uv が見つかりません" >&2; exit 1; }
path_dirs="$(dirname "$uv_path")"
if ffmpeg_path=$(command -v ffmpeg); then
  path_dirs="$path_dirs:$(dirname "$ffmpeg_path")"
else
  echo "注意: ffmpeg が見つかりません。サムネイルは作成されません" >&2
fi
path_dirs="$path_dirs:/usr/bin:/bin:/usr/sbin:/sbin"

# ask 変数名 質問 既定値: オプション指定済みなら聞かない。空欄なら既定値
ask() {
  local var="$1" label="$2" default="$3" answer
  [ -n "${!var}" ] && return
  if $interactive; then
    read -rp "$label [$default]: " answer
    printf -v "$var" '%s' "${answer:-$default}"
  else
    printf -v "$var" '%s' "$default"
  fi
}

iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
default_allow=""
if lan_ip=$(ipconfig getifaddr "$iface" 2>/dev/null); then
  mask=$(ipconfig getoption "$iface" subnet_mask)
  gateway=$(route -n get default | awk '/gateway:/{print $2}')
  default_allow="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1] + "/" + sys.argv[2]).network)' "$lan_ip" "$mask")${gateway:+,!$gateway}"
fi

$interactive && echo "LaunchAgentの設定（Enterで [ ] 内の値を使います）"
ask name "Macのホスト名（.localで開く名前）" "$(scutil --get LocalHostName)"
ask allowip "許可する接続元（!は除外）" "${default_allow:-自動（起動時のLAN）}"
[ "$allowip" = "自動（起動時のLAN）" ] && allowip=""
ask port "待ち受けポート" 8765
case "$port" in ''|*[!0-9]*) echo "ポートは数字で指定してください: $port" >&2; exit 2 ;; esac
ask data "データの保存先" "$HOME/Library/Application Support/webvideo"

args=(-name "$name" -port "$port")
[ -n "$allowip" ] && args+=(-allowip "$allowip")

admin="${DLNA_ADMIN_TOKEN:-${DLNA_TOKEN:-}}" guest="${DLNA_GUEST_TOKEN:-}" generated=false
if $dry; then
  admin="${admin:-dry-run}"
else
  if [ -z "$admin" ]; then
    $interactive || { echo "非対話で実行する場合は DLNA_ADMIN_TOKEN を設定してください" >&2; exit 1; }
    read -rsp "管理者トークン（空欄で自動生成）: " admin; echo
    [ -n "$admin" ] || { admin=$(openssl rand -hex 24); generated=true; }
  fi
  if [ -z "${DLNA_GUEST_TOKEN+x}" ] && $interactive; then
    read -rsp "ゲストトークン（空欄でゲストなし）: " guest; echo
  fi
  if [ -n "$guest" ] && [ "$guest" = "$admin" ]; then
    echo "ゲストトークンには管理者トークンと別の値を指定してください" >&2; exit 1
  fi
fi

echo
echo "  ホスト名:   $name"
echo "  許可接続元: ${allowip:-起動時のLANサブネット（ルーター除外）}"
echo "  ポート:     $port"
echo "  データ:     $data"
echo "  ゲスト:     $([ -n "$guest" ] && echo 有効 || echo 無効)"
if $interactive && ! $dry; then
  loaded && echo "  ※ 登録済みの設定を置き換えます"
  read -rp "この内容で登録しますか？ [Y/n]: " ok
  case "$ok" in ''|[Yy]*) ;; *) echo "中止しました"; exit 1 ;; esac
fi

# plistlib で書き出す（XMLエスケープのため）。トークンは引数でなく環境変数で渡し ps に出さない
write_plist() {
  (umask 077
   PL_OUT="$1" PL_LABEL="$LABEL" PL_ROOT="$ROOT" PL_LAUNCHER="$LAUNCHER" PL_PATH="$path_dirs" PL_DATA="$data" PL_LOG="$LOG" \
   PL_ADMIN="$2" PL_GUEST="$3" python3 - "${args[@]}" <<'EOF'
import os, plistlib, sys
e = os.environ
env = {"PATH": e["PL_PATH"], "DLNA_DATA": e["PL_DATA"], "DLNA_ADMIN_TOKEN": e["PL_ADMIN"]}
if e["PL_GUEST"]:
    env["DLNA_GUEST_TOKEN"] = e["PL_GUEST"]
with open(e["PL_OUT"], "wb") as f:
    plistlib.dump({"Label": e["PL_LABEL"], "ProgramArguments": [e["PL_LAUNCHER"], e["PL_ROOT"] + "/launch.sh", *sys.argv[1:]],
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
  echo "== ${PLIST}（登録はしていません）"
  cat "$tmp"
  exit 0
fi

# 登録済みなら、今の設定のポート（-port の次の引数）。自分のサーバーが使っているポートは使用中とみなさない
current_port=""
if loaded && [ -e "$PLIST" ]; then
  current_port=$(plutil -extract ProgramArguments json -o - "$PLIST" 2>/dev/null |
    python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[a.index("-port")+1] if "-port" in a[:-1] else "")' 2>/dev/null || true)
fi
if [ "$port" != "$current_port" ] && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ポート $port は既に使われています。手動で起動したサーバーなどを止めるか、別のポートを指定してください" >&2
  exit 1
fi

# 起動に使う環境が壊れていないか（プロジェクト移動後の古い .venv など）を登録前に確認する
if ! (cd "$ROOT" && PATH="$path_dirs" uv run --frozen uvicorn --version >/dev/null 2>&1); then
  echo "uvicorn を起動できません。次を実行してから再度登録してください:" >&2
  echo "  (cd \"$ROOT\" && uv sync --locked --reinstall)" >&2
  exit 1
fi

if [ ! -e "$data/catalog.sqlite3" ] && [ -e "$ROOT/data/catalog.sqlite3" ]; then
  mkdir -p "$data"
  cp -Rn "$ROOT/data/." "$data/"
  echo "既存データを引き継ぎました: $ROOT/data -> $data"
fi
mkdir -p "$data" "$(dirname "$PLIST")" "$(dirname "$LOG")"

# 登録済みなら外し、launchd から消えるまで待つ（残ったままだと bootstrap が失敗する）
if loaded; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do loaded || break; sleep 1; done
  if loaded; then
    echo "登録済みの $LABEL を停止できませんでした。./install-daemon.sh uninstall を実行してから再度登録してください" >&2
    exit 1
  fi
fi
# ソースが前回と同じなら作り直さない（署名が変わるとローカルネットワークの許可を求め直されるため）
"$ROOT/launcher/build.sh" "$APP"
write_plist "$PLIST" "$admin" "$guest"
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
  echo "launchd への登録に失敗しました。./install-daemon.sh uninstall を実行してから再度登録してください" >&2
  exit 1
fi

echo "登録して起動しました: $LABEL"
$generated && echo "  管理者トークン（ログインに使います。控えてください）: $admin"
echo "  設定: ${PLIST}（本人のみ読み書き可）"
echo "  データ: $data"
echo "  ログ: $LOG"
echo "初回は「FamilyVideos」がローカルネットワークへのアクセス許可を求めます。「許可」を選んでください。"
echo "  （後から変更: システム設定 → プライバシーとセキュリティ → ローカルネットワーク）"
echo "状態確認: ./install-daemon.sh status / 停止・解除: ./install-daemon.sh uninstall"
