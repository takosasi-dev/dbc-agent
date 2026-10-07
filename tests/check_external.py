"""外部 API を実際に叩いて、今も取れることと形が変わっていないかを見る。

    python tests/check_external.py

**これは run_all.py に入れていない。** ネットワークと相手のサイトに依存するので、
落ちたときに自分のコードのせいなのか相手のせいなのか分からなくなる。
パーサの検証は tests/test_collectors.py が写し(fixture)に対して行う。

使うのは登録不要・無料のものだけ。取得間隔は本番では6時間なので、
この確認を短い間隔で何度も回さないこと。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from svcscope import collectors as col  # noqa: E402
from svcscope.config import Config  # noqa: E402

# 実際に叩くので許可リストを開ける。既定の設定では空で、外へ出ない
ALLOW = [
    "https://security.archlinux.org/",
    "https://archlinux.org/feeds/news/",
]


def check_security() -> None:
    c = col.Security(Config(external_allow=ALLOW))
    began = time.monotonic()
    raw = c._fetch(col.Security.URL)
    issues = json.loads(raw)
    took = time.monotonic() - began

    assert isinstance(issues, list) and issues, "一覧が空です"
    keys = set(issues[0])
    need = {"name", "packages", "status", "severity", "type", "fixed", "issues"}
    missing = need - keys
    assert not missing, f"項目が消えています: {sorted(missing)}"

    vuln = [x for x in issues if "Vulnerable" in (x.get("status") or "")]
    sev = sorted({x.get("severity") for x in issues})
    unknown = set(sev) - set(col._AVG_SEVERITY)
    assert not unknown, f"知らない severity が増えています: {sorted(unknown)}"

    print(f"  OK security  {len(raw) / 1024:.0f}KB  {took:.1f}s  "
          f"AVG {len(issues)} 件 / 未修正 {len(vuln)} 件  severity={sev}")

    # 入っている物が1つも無い環境でも、形の確認だけはできる
    built = c.build(issues, {"linux", "openssl", "curl", "bash"}, 0)
    print(f"     仮の4パッケージで照合 -> {built['vulnerable_count']} 件")


def check_news() -> None:
    c = col.News(Config(external_allow=ALLOW))
    began = time.monotonic()
    raw = c._fetch(col.News.URL)
    took = time.monotonic() - began
    built = c.build(raw, time.time())

    print(f"  OK news      {len(raw) / 1024:.0f}KB  {took:.1f}s  "
          f"直近{c.WINDOW_DAYS}日 {len(built['alerts'])} 件")
    for a in built["alerts"][:3]:
        when = time.strftime("%Y-%m-%d", time.localtime(a["ts"] / 1000))
        print(f"     [{a['severity']:<7}] {when} {a['message'][:70]}")


def check_endoflife() -> None:
    """endoflife.date は v0.1 では使っていない。取れることだけ見ておく。

    ローリングリリースの Arch では「サポート終了」が当たらないので、
    実装は後回しにした(alert の source には eol を残してある)。
    """
    c = col.News(Config(external_allow=["https://endoflife.date/api/"]))
    url = "https://endoflife.date/api/v1/products/linux/"
    raw = c._fetch(url)
    body = json.loads(raw)
    cycles = (body.get("result") or {}).get("releases") or []
    print(f"  -- eol       {len(raw) / 1024:.0f}KB  linux の cycle {len(cycles)} 件"
          f"  (v0.1 では未実装)")


def main() -> int:
    print("外部 API を実際に叩きます(登録不要・無料のものだけ)")
    failed = 0
    for fn in (check_security, check_news, check_endoflife):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - どれが落ちても他は見る
            failed += 1
            print(f"  NG {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    if failed:
        print(f"\n{failed} 件が失敗しました。相手側の変更か、ネットワークの問題かを切り分けること",
              file=sys.stderr)
        return 1
    print("\n外部 API はすべて取れました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
