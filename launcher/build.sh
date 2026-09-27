#!/usr/bin/env bash
# FamilyVideos.app（LaunchAgent の起動用アプリ）をビルドし、この Mac 用に ad-hoc 署名する。
# 使い方: launcher/build.sh 出力先.app
# ソースが前回と同じなら作り直さない（署名が変わるとローカルネットワークの許可を求め直されるため）。
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
app="${1:?出力先の .app を指定してください}"
stamp="$app/Contents/Resources/source.sha256"
hash=$(cat "$src/FamilyVideos.c" "$src/Info.plist" | shasum -a 256 | cut -d' ' -f1)
if [ -x "$app/Contents/MacOS/FamilyVideos" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$hash" ] &&
   codesign --verify "$app" 2>/dev/null; then
  exit 0
fi
command -v clang >/dev/null || { echo "clang が見つかりません。xcode-select --install を実行してください" >&2; exit 1; }
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$src/Info.plist" "$app/Contents/Info.plist"
clang -O2 -Wall -Wextra -mmacosx-version-min=13.0 -o "$app/Contents/MacOS/FamilyVideos" "$src/FamilyVideos.c"
echo "$hash" > "$stamp"
codesign --force --sign - --identifier local.familyvideos "$app"
echo "ビルドしました: $app"
