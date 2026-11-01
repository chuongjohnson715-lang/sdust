# -*- coding: utf-8 -*-
"""Plan B 直链新鲜度守卫自测（不需要登录）。

默认用一条**合成的过期直链**跑完全部断言；想用真实 tracks.json 时传路径：
    python scripts/test_light_common.py [path/to/tracks.json]

验证：
  1. url_ts / url_age_s 能从真实直链解出 ts 并算出签名年龄
  2. is_url_fresh 对「已过期直链」判 False、对「刚签发直链」判 True
  3. preflight 对过期直链明确报 403（而不是含糊的网络错误）
  4. assert_fresh 对过期直链抛错，错误消息含「过期」
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import light_common as LC  # noqa: E402

TRACKS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
# 没有真实配置时用合成直链：1 小时前签发，指向一个肯定连不上的本地端口，
# 只验证「ts 解析 / 新鲜度判定 / 过期报错」，不依赖任何真实存储节点。
SYNTHETIC_URL = (
    "http://127.0.0.1:9/lesson.mp4?ts=" + str(int(time.time()) - 3600) + "&token=demo"
)

ok_all = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_all
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok_all = False
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")


def skip(name: str, why: str = "") -> None:
    print(f"  [SKIP] {name}{(' — ' + why) if why else ''}")


def main() -> int:
    print("=== Plan B 直链新鲜度守卫自测 ===")
    if TRACKS:
        tracks = json.loads(TRACKS.read_text(encoding="utf-8"))
        url = tracks["INSTRUCTOR"]["download_url"]
        print(f"  直链来源：{TRACKS}")
    else:
        url = SYNTHETIC_URL
        print("  直链来源：合成样例（1 小时前签发）")

    # 1) ts 解析
    ts = LC.url_ts(url)
    check("url_ts 能解出 ts", ts is not None, f"ts={ts}")
    age = LC.url_age_s(url)
    check("url_age_s 能算出签名年龄", age is not None,
          f"{age/3600:.1f} 小时前" if age is not None else "")
    if ts:
        import datetime
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        check("ts 解析为合理时间", 2020 < dt.year < 2100, dt.isoformat())

    # 2) 新鲜度判定
    check("过期直链 is_url_fresh=False", not LC.is_url_fresh(url))
    fresh = re.sub(r"([?&]ts=)\d+", rf"\g<1>{int(time.time())}", url)
    check("刚签发直链 is_url_fresh=True", LC.is_url_fresh(fresh))

    # 3) preflight 报 403
    # 样例直链指向 tc2:8804，存储节点随时会下线；连不上时只能跳过，
    # 不能把「主机不可达」当成「403 判定失灵」。
    ok_pf, why = LC.preflight(url)
    if "HTTP 0" in why or "URLError" in why:
        skip("preflight 403 判定（样例存储节点不可达）", why[:70])
    else:
        check("preflight 对过期直链判不可用", not ok_pf, why)
        check("preflight 明确指出 403", "403" in why, why[:80])

    # 4) assert_fresh 抛错
    raised = False
    try:
        LC.assert_fresh(url)
    except RuntimeError as e:
        raised = True
        check("assert_fresh 错误消息含「过期」", "过期" in str(e), str(e)[:90])
    check("assert_fresh 对过期直链抛 RuntimeError", raised)

    # 5) 未知 ts 不误杀
    nots = "http://example.com/a.mp4?token=abc"
    check("无 ts 时 is_url_fresh=True（不误杀）", LC.is_url_fresh(nots))
    check("无 ts 时 url_age_s=None", LC.url_age_s(nots) is None)

    print("\n=== " + ("全部通过" if ok_all else "存在失败项") + " ===")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
