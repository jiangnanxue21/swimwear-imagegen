r"""实时查看运行日志(`docs/LOG-CONSOLE.md` §5.3)。

用法(仓库根目录):

    backend\.venv\Scripts\python.exe backend\tools\watch_logs.py
    python3 backend/tools/watch_logs.py --domain llm,gen --level warning --routine
    python3 backend/tools/watch_logs.py --ring          # 直接读 Redis 环形缓冲

## 它取代了 `watch_ai_logs.py`,而换掉的是那个文件最坏的一个决定

原来的过滤集是 9 条**硬编码的消息原文**:

    AI_MESSAGES = {"llm request prepared", "llm http response received", ...}

消息措辞一改,查看器就**安静漏事件** —— 不报错、不提示,就是少了。而 message
本来就该是一句给人读的话,想怎么改就怎么改。现在过滤从
`app.core.log_events` 取,和 Web 页用的是同一套注册表、同一套语义:
一个人在终端学会的过滤,换到页面上不用重学。

`tests/pure/test_a53_log_console.py` 反向钉着这一点 —— 本文件源码里
**不许出现任何调用点的 message 原文**。

## 保留的两个决定

只读日志,不启动、不停止 API;按本机首选编码读取(Windows 下 API 重定向文件
通常使用系统代码页)。单行 JSON 解析失败时只报告位置,不把整条 2MB 请求
重复刷到屏幕上。
"""
from __future__ import annotations

import argparse
import json
import locale
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.log_events import (  # noqa: E402 - 必须先把 backend 放进 sys.path
    DOMAINS,
    EVENTS,
    folds_away,
    label_of,
    level_at_least,
    resolve_domain,
)

DEFAULT_LOG = BACKEND_ROOT / ".api-stdout.log"

#: 不带参数时的默认过滤。**向后兼容 `watch_ai_logs.py` 今天的用途** ——
#: 那个脚本存在的理由就是"盯着模型调用",默认换成全量会让老用法变成刷屏。
DEFAULT_DOMAINS = ("llm",)

#: 展开显示的载荷字段。它们只在 `LLM_LOG_PAYLOADS=true` 时存在 ——
#: 默认关,而**默认关是对的**(归档面不该躺着商品数据)。想看原文走
#: `/api/ops/llm/{call_id}`,那是另一个去向、另一套寿命。
_PAYLOAD_KEYS = ("request_body", "response_body")

_HIDDEN = {"ts", "level", "logger", "message", "domain", "event", "seq", *_PAYLOAD_KEYS}


def _encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def _compact(value: Any, *, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + f"…({len(text)} chars)"


def _print_payload(label: str, payload: Any) -> None:
    print(f"\n  {label}:")
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    for line in pretty.splitlines():
        print(f"    {line}")


class Filter:
    """一次查看的过滤条件。**取值全部来自注册表,没有一条写死的消息原文。**"""

    def __init__(
        self,
        *,
        domains: tuple[str, ...] = DEFAULT_DOMAINS,
        events: tuple[str, ...] = (),
        min_level: str = "DEBUG",
        show_routine: bool = False,
        text: str = "",
    ) -> None:
        self.domains = tuple(d for d in domains if d)
        self.events = tuple(events)
        self.min_level = min_level.upper()
        self.show_routine = show_routine
        self.text = text.lower()

    def accepts(self, row: dict[str, Any]) -> bool:
        event = row.get("event")
        domain = row.get("domain") or resolve_domain(event, row.get("logger"))
        level = str(row.get("level") or "INFO").upper()

        if self.domains and domain not in self.domains:
            return False
        if self.events and event not in self.events:
            return False
        # 级别与折叠都走注册表里那一份判定 —— Web 页调的是同两个函数。
        # 上一版这里自己存了一张级别序表,而 API 那边是精确匹配;
        # 同一个词在两个入口两种意思,正是这套设计要消灭的东西。
        if not level_at_least(level, self.min_level):
            return False
        # 例行事件默认收起,但 **ERROR / CRITICAL 永不折叠**。少了这一条,
        # 一个被标成例行的事件在真的出错时也会被藏起来,而那正是最不该藏的一次。
        if not self.show_routine and folds_away(event, level):
            return False
        if self.text and self.text not in json.dumps(row, ensure_ascii=False).lower():
            return False
        return True


def _print_event(row: dict[str, Any], flt: Filter | None = None) -> None:
    flt = flt or Filter(domains=(), show_routine=True)
    if not flt.accepts(row):
        return

    event = row.get("event")
    domain = row.get("domain") or resolve_domain(event, row.get("logger"))
    label = label_of(event)

    print("\n" + "=" * 96)
    headline = f"[{row.get('ts', '-')}] {row.get('level', '-')} {DOMAINS.get(domain, domain)}"
    if label:
        headline += f" · {label}"
    print(headline)
    print(f"  {row.get('message', '')}")
    print(
        f"  event={event or '-'}  request_id={row.get('request_id', '-')}  "
        f"llm_call_id={row.get('llm_call_id', '-')}"
    )

    fields = {k: v for k, v in row.items() if k not in _HIDDEN and v is not None}
    fields.pop("request_id", None)
    for key, value in fields.items():
        print(f"  {key}: {_compact(value)}")

    for key in _PAYLOAD_KEYS:
        if key in row:
            _print_payload(key, row[key])
    sys.stdout.flush()


def _follow_file(path: Path, flt: Filter) -> None:
    encoding = _encoding()
    print(f"运行日志:{path}")
    print(f"读取编码:{encoding};Ctrl+C 只停止查看,不停止 API。")
    print(f"过滤:域={','.join(flt.domains) or '全部'} 级别≥{flt.min_level} "
          f"例行={'展开' if flt.show_routine else '收起'}")
    sys.stdout.flush()

    with path.open("r", encoding=encoding, errors="replace") as stream:
        stream.seek(0, 2)
        while True:
            line = stream.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"\n[查看器] 跳过无法解析的日志行:JSON 第 {exc.pos} 字符处错误")
                continue
            if isinstance(row, dict):
                _print_event(row, flt)


def _follow_ring(flt: Filter, *, interval: float = 2.0) -> None:
    """直接读 Redis 环形缓冲。

    和 Web 页读的是**同一个键**,所以终端和页面看到的是同一份事实。
    环形是诊断窗口不是归档:滚出窗口的记录这里也没有,那时候该去看
    stdout 的采集端。
    """
    from app.core.config import settings
    from app.core.log_ring import read_ring

    print(f"运行日志:Redis 环形缓冲(cap={settings.OPS_LOG_RING_CAP})")
    # 去重表**有界**:环形本身最多 cap 条,一条滚出窗口之后就再也不会回来,
    # 所以记忆只需要覆盖一个窗口。上一版是个只增不减的 set —— 一个挂着跑几天
    # 的查看器会把每一条见过的日志永远留在内存里,而这个脚本的用法恰恰是
    # "开着不管"。
    seen: deque[str] = deque(maxlen=max(1, settings.OPS_LOG_RING_CAP * 2))
    seen_index: set[str] = set()
    while True:
        rows, error = read_ring(settings.REDIS_URL, limit=settings.OPS_LOG_RING_CAP)
        if error:
            print(f"[查看器] 环形缓冲读不到:{error}")
            time.sleep(interval)
            continue
        for row in reversed(rows):
            marker = str(row.get("seq") or f"{row.get('ts')}|{row.get('message')}")
            if marker in seen_index:
                continue
            if len(seen) == seen.maxlen:
                seen_index.discard(seen[0])
            seen.append(marker)
            seen_index.add(marker)
            _print_event(row, flt)
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="实时查看运行日志")
    parser.add_argument("path", nargs="?", default=None, help="日志文件;不给则用后端默认输出")
    parser.add_argument(
        "--domain",
        default=",".join(DEFAULT_DOMAINS),
        help=f"逗号分隔,取值:{','.join(DOMAINS)};写 all 表示不筛",
    )
    parser.add_argument("--event", default="", help="逗号分隔的事件码,精筛用")
    parser.add_argument(
        "--level", default="DEBUG", help="最低级别(含它自己以上)debug/info/warning/error"
    )
    parser.add_argument("--routine", action="store_true", help="展开例行事件(默认收起)")
    parser.add_argument("-q", "--query", default="", help="子串搜索")
    parser.add_argument("--ring", action="store_true", help="读 Redis 环形缓冲而不是日志文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_domains = [d.strip() for d in args.domain.split(",") if d.strip()]
    if "all" in raw_domains:
        domains: tuple[str, ...] = ()
    else:
        unknown = [d for d in raw_domains if d not in DOMAINS]
        if unknown:
            # 打错一个域名不该表现成"这段时间没有日志"。那正是硬编码消息集
            # 最坏的地方 —— 它连打错的机会都不给,直接静默漏掉。
            print(f"未知的域:{','.join(unknown)};可用:{','.join(DOMAINS)}", file=sys.stderr)
            return 2
        domains = tuple(raw_domains)

    events = tuple(e.strip() for e in args.event.split(",") if e.strip())
    unknown_events = [e for e in events if e not in EVENTS]
    if unknown_events:
        # 与未知域同一个口径:打错一个码不该表现成"这段时间没有日志"。
        # 上一版只校验了域,事件码打错会安静地筛出零条。
        print(f"未知的事件码:{','.join(unknown_events)}", file=sys.stderr)
        return 2

    flt = Filter(
        domains=domains,
        events=events,
        min_level=args.level,
        show_routine=args.routine,
        text=args.query,
    )

    try:
        if args.ring:
            _follow_ring(flt)
            return 0
        path = Path(args.path).resolve() if args.path else DEFAULT_LOG
        if not path.exists():
            print(f"日志文件不存在:{path}", file=sys.stderr)
            return 2
        _follow_file(path, flt)
    except KeyboardInterrupt:
        print("\n已停止查看;API 未停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
