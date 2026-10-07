"""全部の検査を順に走らせる。

    python tests/run_all.py

枠組みは入れていない。各モジュールの `demo()` が自分の自己検査を持ち、
tests/ の2本がパーサと API の契約を見る。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# モジュール自身の自己検査。`python -m <名前>` で走る
SELF_CHECKS = [
    ["-m", "svcscope.ring"],
    ["-m", "svcscope.auth"],
    ["-m", "svcscope.config"],
    ["-m", "svcscope.collectors"],
    ["-m", "svcscope.sampler"],
    ["-m", "svcscope.server"],
    ["-m", "svcscope.cli", "--self-check"],
]

TEST_FILES = [
    ["tests/test_collectors.py"],
    ["tests/test_contract.py"],
]


def run(args: list[str]) -> bool:
    label = " ".join(args)
    proc = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        print(f"[ OK ] {label}")
        return True
    print(f"[ NG ] {label}")
    print((proc.stdout + proc.stderr).rstrip())
    return False


def main() -> int:
    # fixture が無いと collector の検査が動かないので先に作る
    if not run(["tests/make_fixtures.py"]):
        return 1

    failed = [a for a in SELF_CHECKS + TEST_FILES if not run(a)]
    total = len(SELF_CHECKS) + len(TEST_FILES)
    if failed:
        print(f"\n{len(failed)} / {total} 件が失敗しました")
        return 1
    print(f"\n{total} / {total} 件 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
