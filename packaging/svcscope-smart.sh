#!/bin/sh
# SMART の値を /run/svcscope/smart.json に書く。root で動く systemd timer から呼ぶ。
#
# エージェント本体に root を持たせないための中継。エージェントはこのファイルを
# 読むだけで、sudo も smartctl も呼ばない(仕様書「認証とセキュリティ」)。
# /run は tmpfs なので HDD への書き込みも増えない。
set -eu

OUT_DIR=/run/svcscope
OUT="$OUT_DIR/smart.json"
TMP="$OUT_DIR/.smart.json.$$"
OWNER=svcscope

# 対象デバイス。未指定なら /sys/block から回転・固定ディスクだけ拾う
DEVICES="${SVCSCOPE_SMART_DEVICES:-}"
if [ -z "$DEVICES" ]; then
  for d in /sys/block/*; do
    name=$(basename "$d")
    case "$name" in
      loop*|ram*|zram*|dm-*|md*|sr*) continue ;;
    esac
    [ -e "/dev/$name" ] && DEVICES="$DEVICES /dev/$name"
  done
fi

if ! command -v smartctl >/dev/null 2>&1; then
  echo "smartctl がありません (pacman -S smartmontools)" >&2
  exit 1
fi

install -d -m 0755 "$OUT_DIR"
printf '{\n  "ts": %s,\n  "devices": {' "$(date +%s000)" > "$TMP"

first=1
for dev in $DEVICES; do
  [ "$first" = 1 ] || printf ',' >> "$TMP"
  first=0
  printf '\n    "%s": ' "$dev" >> "$TMP"
  # smartctl は「異常あり」を終了コードのビットで伝えるので、
  # 0 以外でも出力は使える。set -e に殺されないよう || true で受ける
  out=$(smartctl -j -a "$dev" 2>/dev/null || true)
  if [ -n "$out" ]; then
    printf '%s' "$out" >> "$TMP"
  else
    printf '{"svcscope_error": "smartctl が出力を返しませんでした"}' >> "$TMP"
  fi
done
printf '\n  }\n}\n' >> "$TMP"

# 壊れた JSON をエージェントに読ませない。差し替えは原子的に行う
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$TMP" || {
    echo "生成した JSON が壊れています。差し替えません" >&2
    rm -f "$TMP"
    exit 1
  }
fi

chown "$OWNER" "$TMP" 2>/dev/null || true
chmod 0644 "$TMP"
mv -f "$TMP" "$OUT"
