#!/bin/sh
# 実機の /proc・/sys の写しを2時点ぶん取る。テスト用の fixture にする。
#
#   ./packaging/capture-fixtures.sh tests/fixtures/real
#
# tests/make_fixtures.py が作るのは手書きの模擬データなので、実機で取れた
# ものに置き換えたほうがパーサの検証として強い(仕様書「開発とテスト方針」
# collector の単体テスト)。
#
# ！ 公開前に中身を見ること。/proc/mounts にはマウント先のパスが、
#    cgroup には動いているサービス名が入る。ホスト名や利用者名が
#    混ざっていないかを確かめてからコミットする。
set -eu

OUT=${1:-tests/fixtures/real}
GAP=${2:-2}

copy_one() {
  src=$1; dst=$2
  [ -r "$src" ] || return 0
  mkdir -p "$(dirname "$dst")"
  cat "$src" > "$dst" 2>/dev/null || true
}

snap() {
  root=$1
  for f in /proc/stat /proc/loadavg /proc/meminfo /proc/diskstats \
           /proc/net/dev /proc/mounts /proc/uptime; do
    copy_one "$f" "$root${f}"
  done
  for k in cpu memory io; do
    copy_one "/proc/pressure/$k" "$root/proc/pressure/$k"
  done

  # /sys/block は「ディスク1本」の判定に使うので、ディレクトリが在ることが要る
  for d in /sys/block/*; do
    [ -d "$d" ] || continue
    mkdir -p "$root$d"
    copy_one "$d/size" "$root$d/size"
    copy_one "$d/mm_stat" "$root$d/mm_stat"
    copy_one "$d/disksize" "$root$d/disksize"
  done

  copy_one /sys/fs/cgroup/cgroup.controllers "$root/sys/fs/cgroup/cgroup.controllers"
  for d in /sys/fs/cgroup/system.slice/*; do
    [ -d "$d" ] || continue
    mkdir -p "$root$d"
    for f in cpu.stat memory.current io.stat; do
      copy_one "$d/$f" "$root$d/$f"
    done
  done
}

rm -rf "$OUT/t0" "$OUT/t1"
echo "1枚目を取ります..."
snap "$OUT/t0"
echo "${GAP} 秒待ちます..."
sleep "$GAP"
echo "2枚目を取ります..."
snap "$OUT/t1"

cat <<EOS

取れました: $OUT/t0 と $OUT/t1 (${GAP} 秒差)

確認:
  python tests/test_collectors.py        # 模擬データでの検証
  python -m svcscope --fixtures $OUT --token-hash <hash> --port 18765

公開前に中身を見てください。マウント先のパス・サービス名・ホスト名が
入っていないか、個人情報が混ざっていないかを確かめてからコミットします。
EOS
