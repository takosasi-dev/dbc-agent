#!/bin/sh
# API トークンを作る。サーバ側で root で1回だけ実行する。
#
# サーバに残るのは SHA-256 のハッシュだけ。平文はこの画面に1回出るだけで
# どこにも保存しない(仕様書「認証とセキュリティ」トークン)。
# 平文は PC 側の権限 600 のファイルに置く。
set -eu

HASH_FILE=/etc/svcscope/token.sha256

if [ "$(id -u)" -ne 0 ]; then
  echo "root で実行してください" >&2
  exit 1
fi

if [ -e "$HASH_FILE" ]; then
  printf 'すでに %s があります。作り直しますか? PC 側のトークンも入れ替えが必要です [y/N] ' "$HASH_FILE"
  read -r answer
  case "$answer" in [yY]*) ;; *) echo "やめました"; exit 0 ;; esac
fi

# 256 ビット。openssl が無い機械でも /dev/urandom から作れるようにする
if command -v openssl >/dev/null 2>&1; then
  TOKEN=$(openssl rand -hex 32)
else
  TOKEN=$(od -An -vtx1 -N32 /dev/urandom | tr -d ' \n')
fi

# 末尾の改行を含めない。エージェント側も改行を取り除いてから照合する
HASH=$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)

install -d -m 0750 /etc/svcscope
umask 027
printf '%s\n' "$HASH" > "$HASH_FILE"
chmod 0640 "$HASH_FILE"
# エージェントのユーザが読めるように。まだ居なければ root のままでよい
chown root:svcscope "$HASH_FILE" 2>/dev/null || true

cat <<'EOS'

--------------------------------------------------------------------
PC 側に設定するトークンは次の1行です。この画面を閉じると二度と出ません。
PC 側では ~/.config/svcscope/token に置き、chmod 600 してください。
--------------------------------------------------------------------
EOS
printf '%s\n' "$TOKEN"
cat <<EOS
--------------------------------------------------------------------
サーバに保存したのはハッシュだけです: $HASH_FILE

シェルの履歴にトークンが残らないよう、この後 history をクリアするか、
履歴を残さない設定で実行してください。
--------------------------------------------------------------------
EOS
