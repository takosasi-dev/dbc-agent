# PC からつなぐ

エージェントは `127.0.0.1:8765` にしか待ち受けない。外からは直接届かないので、
PC から SSH のポートフォワードを張って、その口を PC 側の `127.0.0.1:18765` に
出してから使う。サーバ側にポートを開ける作業は無い。

```
PC                                       サーバ
127.0.0.1:18765  --[ ssh のトンネル ]-->  127.0.0.1:8765
```

このファイルに実際のホスト名・利用者名・IP は書かない。`<host>` などは
自分の環境の値に読み替える。

## 1. 鍵を2本に分ける

管理用とトンネル専用を分ける。トンネル専用の鍵は、万一漏れても
ポートフォワード以外に何もできないように `authorized_keys` 側で縛る。

サーバの `~/.ssh/authorized_keys` に、トンネル用の公開鍵の行頭へこれを付ける。

```
restrict,port-forwarding,permitopen="127.0.0.1:8765",command="/usr/bin/nologin" ssh-ed25519 AAAA... tunnel-only
```

- `restrict` で全部禁止したうえで、`port-forwarding` だけ戻す
- `permitopen` でエージェントのポート以外への転送を拒否する
- `command="/usr/bin/nologin"` でシェルを取らせない

**先に管理用の鍵でログインできることを確かめてから**この行を足す。
順番を逆にすると、入れなくなったときに戻す手段が無くなる。

## 2. PC 側の ~/.ssh/config

Windows では `%USERPROFILE%\.ssh\config`。使う ssh は Windows 標準の
`C:\Windows\System32\OpenSSH\ssh.exe` で、WSL の中の ssh ではない。

```
Host arch-server
    HostName        <サーバのアドレス>
    User            <利用者名>
    IdentityFile    ~/.ssh/id_ed25519_arch_admin
    IdentitiesOnly  yes

Host arch-tunnel
    HostName        <サーバのアドレス>
    User            <利用者名>
    IdentityFile    ~/.ssh/id_ed25519_arch_tunnel
    IdentitiesOnly  yes
    # 落ちたトンネルを掴んだままにしない
    ServerAliveInterval   15
    ServerAliveCountMax   3
    # 転送に失敗したら黙って繋がったふりをしない
    ExitOnForwardFailure  yes
```

ホスト鍵は初回に自分で確認する。`StrictHostKeyChecking=no` は使わない。
中間者を黙って受け入れる設定で、トークンを流す経路には置けない。

## 3. トンネルを張る

```
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 arch-tunnel
```

- `-N` はコマンドを実行せず転送だけする
- 左側を `127.0.0.1:18765` と明示する。`18765` だけ書くと環境によって
  全インターフェースに出てしまい、同じ LAN の他の機械から見える

張れているかは、別の窓でこれを打って確かめる。

```
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18765/version
```

`401` が返れば正しい。トンネルは通っていて、トークンが無いだけという意味。
`000` や `couldn't connect` ならトンネルが張れていない。

## 4. CUI で見る

トークンの平文は PC 側だけに置く。`~/.config/svcscope/token` に1行で書き、
自分以外が読めないようにする(Linux なら `chmod 600`)。

```
# 1回だけ見る
python -m svcscope.cli --url http://127.0.0.1:18765 snapshot

# 2秒ごとに上書き表示(Ctrl-C で止める)
python -m svcscope.cli --url http://127.0.0.1:18765 watch

# そのままの JSON
python -m svcscope.cli --url http://127.0.0.1:18765 --json snapshot
```

サーバに ssh で入ってそのまま叩くなら、トンネルは要らない。

```
python -m svcscope.cli --token-file ~/svcscope-token snapshot
```

## 5. つながらないときの順番

1. `ssh arch-tunnel` 単体でつながるか(鍵と config の問題)
2. `ssh -N -L ...` が `administratively prohibited` で落ちないか
   → サーバの `sshd -T | grep -i allowtcpforwarding` が `yes` か、
     `authorized_keys` の `permitopen` がエージェントのポートと一致しているか
3. サーバ内で `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/version`
   が `401` を返すか(エージェントが動いているか)
4. `systemctl status svcscope` と `journalctl -u svcscope -n 50`
5. トークンの平文とサーバ側のハッシュが対応しているか
   → 合わないときは `gen-token.sh` で作り直し、両方入れ替える

## 6. 将来

WireGuard に替えるときは、エージェントの `bind` を `wg0` の IP にして、
CUI/GUI のつなぎ先を差し替えるだけでよい。API は変えない。
