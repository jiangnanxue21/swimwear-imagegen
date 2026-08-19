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
    normalize_level,
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

_HIDDEN = {
    "ts", "level", "logger", "message", "domain", "event", "seq",
    # 这两个有自己的固定位置(见 `_print_event`),不再混进字段列表
    "service", "pid",
    *_PAYLOAD_KEYS,
}


#: 日志文件的编码。**固定 UTF-8,与写入端同一个口径。**
#:
#: 上一版是 `locale.getpreferredencoding(False)` —— 而 `core/logging.py` 的
#: `setup_logging` 有一整段注释在讲为什么必须强制 UTF-8:
#:
#:     中文 Windows 上 stdout 默认是 GBK,轻则整份日志不是 UTF-8、采集端
#:     读出乱码,重则遇到一个 GBK 编不出的字符抛 UnicodeEncodeError ——
#:     logging 会吞掉它,**整条记录消失**。而这类记录往往正是出问题的那一条。
#:
#: **写的时候强制 UTF-8,读的时候用系统代码页。** 在中文 Windows 上,
#: 所有中文 message 与字段值会变成乱码 —— 而 JSON 结构是纯 ASCII,
#: `json.loads` 照样成功,**不报任何错**。查看器于是安静地展示乱码,
#: 而人会以为是日志本身写坏了。
#:
#: `errors="replace"` 保留:一条坏字节不该让整个查看器停下来。
LOG_ENCODING = "utf-8"


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
        service: str = "",
    ) -> None:
        self.domains = tuple(d for d in domains if d)
        self.events = tuple(events)
        self.min_level = min_level.upper()
        self.show_routine = show_routine
        self.text = text.lower()
        #: 哪个进程写的。**与 Web 同名同义** —— 一个人在终端学会的过滤,
        #: 换到页面上不用重学(§5.3 那条口径,这里是它的第二个字段)。
        self.service = service.strip()

    def accepts(self, row: dict[str, Any]) -> bool:
        event = row.get("event")
        domain = row.get("domain") or resolve_domain(event, row.get("logger"))
        level = str(row.get("level") or "INFO").upper()

        if self.domains and domain not in self.domains:
            return False
        if self.service and str(row.get("service") or "") != self.service:
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
    # 哪个进程写的。三种进程写进同一个环形,少了这一列就分不出
    # 「gen 域为什么空着」是没跑任务还是 worker 挂了。
    print(f"  service={row.get('service', '-')}  pid={row.get('pid', '-')}")

    fields = {k: v for k, v in row.items() if k not in _HIDDEN and v is not None}
    fields.pop("request_id", None)
    # 表头那一行已经打过它了 —— 再随字段区打一遍,同一屏两份同一个值,
    # 读的人会以为它们可能不同。
    fields.pop("llm_call_id", None)
    # 堆栈**不进 160 字符的 compact 预算**。exc 是排障最要紧的字段,而
    # `_compact` 会把一条 Traceback 截成
    # `"Traceback (most recent call last):\n  File …(2489 chars)"` ——
    # 真正报错的最后一行恰恰在被截掉的那一截里。全文缩进打印,
    # 与 `_print_payload` 同一个姿势。
    exc_text = fields.pop("exc", None)
    for key, value in fields.items():
        print(f"  {key}: {_compact(value)}")
    if isinstance(exc_text, str) and exc_text.strip():
        print("  exc:")
        for line in exc_text.splitlines():
            print(f"    {line}")

    for key in _PAYLOAD_KEYS:
        if key in row:
            _print_payload(key, row[key])
    sys.stdout.flush()


def _replay_tail(stream: Any, flt: Filter, count: int) -> None:
    """先把最后 ``count`` 条回放出来,再进入跟随。

    上一版 `_follow_file` 开头直接 `seek(0, 2)` 跳到文件末尾,于是 CLI
    **完全看不了历史** —— 只能等新日志。而这个工具最常见的用法恰恰是
    「刚才出的那个错,给我看看」:等你把命令敲完,那条日志已经过去了。

    `deque(maxlen=)` 而不是 `readlines()[-N:]`:日志文件是只增的,
    整份读进内存这件事会在某个跑了两周的开发机上变成一次 OOM。
    """
    kept: deque[str] = deque(maxlen=max(0, count))
    for line in stream:
        kept.append(line)
    if not kept:
        return
    print(f"—— 回放最近 {len(kept)} 行 ——")
    for line in kept:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            _print_event(row, flt)
    print("—— 回放结束,以下是新日志 ——")
    sys.stdout.flush()


def _follow_file(path: Path, flt: Filter, *, tail: int = 200) -> None:
    print(f"运行日志:{path}")
    print(f"读取编码:{LOG_ENCODING}(与写入端一致);Ctrl+C 只停止查看,不停止 API。")
    print(f"过滤:域={','.join(flt.domains) or '全部'} 级别≥{flt.min_level} "
          f"例行={'展开' if flt.show_routine else '收起'}"
          f"{' 进程=' + flt.service if flt.service else ''}")
    sys.stdout.flush()

    with path.open("r", encoding=LOG_ENCODING, errors="replace") as stream:
        if tail > 0:
            _replay_tail(stream, flt, tail)
        else:
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
        # `read_ring` 现在返回 (原始行, 解析后) —— 原始行留给需要子串匹配的
        # 调用方(见 `api/ops_logs.py`),这里只用解析后的那一半。
        for _raw, row in reversed(rows):
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
    parser.add_argument(
        "--service", default="", help="只看某个进程:api/worker/beat/script"
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=200,
        help="先回放最后 N 行再跟随(默认 200;给 0 表示只看新日志,即旧行为)",
    )
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

    # 与未知域/未知事件码同一个口径:打错一个级别名要当场说出来,不该被
    # `level_rank` 的未知兜底按 INFO 算 —— 那条兜底属于日志行自己的级别,
    # 不属于筛选条件。上一版 `--level warn` 会静默变成"不筛"。
    try:
        min_level = normalize_level(args.level) or "DEBUG"
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    flt = Filter(
        domains=domains,
        events=events,
        min_level=min_level,
        show_routine=args.routine,
        text=args.query,
        service=args.service,
    )

    try:
        if args.ring:
            _follow_ring(flt)
            return 0
        path = Path(args.path).resolve() if args.path else DEFAULT_LOG
        if not path.exists():
            print(f"日志文件不存在:{path}", file=sys.stderr)
            return 2
        _follow_file(path, flt, tail=args.tail)
    except KeyboardInterrupt:
        print("\n已停止查看;API 未停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
