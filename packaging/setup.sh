#!/bin/sh
# Arch Linux のサーバにエージェントを入れる。root で実行する。何度流しても同じ結果になる。
#
# サービスはまだ起動しない。トークンを作る前に起動しても意味がないため、
# 最後に次の手順を表示して終わる。
set -eu

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
TARGET=/opt/dbc
UNIT_DIR=/etc/systemd/system
USER_NAME=dbc

if [ "$(id -u)" -ne 0 ]; then
  echo "root で実行してください" >&2
  exit 1
fi

echo "== 前提の確認 =="

# tomllib が要る(Python 3.11 以降)
if ! python -c 'import sys,tomllib; sys.exit(0)' 2>/dev/null; then
  echo "  NG: python に tomllib がありません。Python 3.11 以降が必要です" >&2
  exit 1
fi
echo "  OK: $(python --version)"

# cgroup v2(unified)前提。v1 では unit 単位の収集ができない
if [ -e /sys/fs/cgroup/cgroup.controllers ]; then
  echo "  OK: cgroup v2"
else
  echo "  NG: cgroup v2 ではありません。unit 単位の収集は非対応になります" >&2
fi

# PSI はこのツールの売り。無効だと「非対応」と返るだけなので、ここで気づけるようにする
if [ -d /proc/pressure ]; then
  echo "  OK: PSI 有効"
else
  echo "  注意: /proc/pressure がありません。カーネルに psi=1 が必要です" >&2
fi

# ssh のポートフォワードが禁止されていると PC から届かない(仕様書の未決事項)
if sshd -T 2>/dev/null | grep -qi '^allowtcpforwarding yes'; then
  echo "  OK: sshd の AllowTcpForwarding は yes"
else
  echo "  注意: sshd の AllowTcpForwarding を確認してください (sshd -T | grep -i forward)" >&2
fi

command -v smartctl >/dev/null 2>&1 \
  && echo "  OK: smartctl" \
  || echo "  注意: smartctl がありません (pacman -S smartmontools)。SMART は非対応になります"

echo "== 利用者とディレクトリ =="
if id "$USER_NAME" >/dev/null 2>&1; then
  echo "  既にある: $USER_NAME"
else
  useradd --system --home-dir "$TARGET" --shell /usr/bin/nologin "$USER_NAME"
  echo "  作成: $USER_NAME"
fi
# journald を読むのに要る(次段の journal collector 用)
usermod -aG systemd-journal "$USER_NAME"

install -d -m 0755 "$TARGET"
install -d -m 0750 /etc/dbc
# git で取ったものをそのまま current にする運用(Python プロトタイプ段階)。
# v0.1 のリリース以降は署名付きの releases/ + シンボリックリンクに移す。
if [ ! -e "$TARGET/current" ]; then
  ln -s "$REPO_DIR" "$TARGET/current"
  echo "  リンク: $TARGET/current -> $REPO_DIR"
else
  echo "  既にある: $TARGET/current -> $(readlink -f "$TARGET/current")"
fi
chmod +x "$REPO_DIR/packaging/dbc-smart.sh"

echo "== 設定 =="
if [ -e /etc/dbc/config.toml ]; then
  echo "  既にある: /etc/dbc/config.toml (上書きしません)"
else
  install -m 0640 "$REPO_DIR/packaging/config.example.toml" /etc/dbc/config.toml
  chown root:"$USER_NAME" /etc/dbc/config.toml
  echo "  作成: /etc/dbc/config.toml"
fi

echo "== systemd =="
for unit in dbc.service dbc-smart.service dbc-smart.timer; do
  install -m 0644 "$REPO_DIR/packaging/$unit" "$UNIT_DIR/$unit"
  echo "  入れた: $UNIT_DIR/$unit"
done
systemctl daemon-reload

cat <<EOS

== 次の手順 ==
1. トークンを作る:        $REPO_DIR/packaging/gen-token.sh
   表示された平文を PC 側の ~/.config/dbc/token に置き chmod 600 する
2. 起動する:              systemctl enable --now dbc.service
3. SMART を有効にする:    systemctl enable --now dbc-smart.timer
4. サーバ内で確認する:    python -m dbc.cli --token-file <平文のファイル> snapshot
5. PC からトンネルを張る: docs/ssh.md を見る

状態の確認: systemctl status dbc / journalctl -u dbc -n 50
EOS
