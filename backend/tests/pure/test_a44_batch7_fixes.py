"""A44 第七批:A-15 · A-32 · C-07 · C-12 · C-15。

这一批之后,A/C 两类里**还能在无真库环境下安全修的都修完了**。
剩下的三类各有各的理由。那份批次评审文档已删(口径见 `docs/DECISIONS.md`
§3.107),三类的划分原样记在下面:

    有意取舍     A-11 / A-12 / C-03 / C-05' / R2-18
    需要真库     C-01 / C-02 / C-06 / C-08 ～ C-10
    需要真前端   A-31(渠道专用集入口)
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _src(path, name: str) -> str:
    """函数的**原始**源码。

    `ast.unparse` 会把引号、括号、空白全部规范化 —— 拿它做字符串匹配时,
    双引号写的字面量在结果里是单引号,断言当场假红。这一批第一次跑就红在
    这上面(C-07 两条)。要匹配写法就读原文,要匹配结构就走 AST 节点。
    """
    return ast.get_source_segment(_read(path), _fn(path, name)) or ""


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name}")


# ==========================================================
# A-15:同一个时间字段不许有两种格式
# ==========================================================

#: 唯一允许的裸 `.isoformat()`:日期键。
#:
#: `spend.py` 用 `bucket.date().isoformat()` 当聚合的键(`2026-08-01`),
#: 那是一个**日期**不是时间戳,套 `iso_utc` 反而会给它加上一个不存在的时刻。
_DATE_KEY = re.compile(r"\.date\(\)\.isoformat\(\)")


def test_every_timestamp_in_the_api_and_service_layer_goes_through_iso_utc():
    """A-15:出参时间戳一律 `iso_utc()`。

    `expire_on_commit=False` 让"刚写完立刻回读"和"刷新页面重新查"序列化出
    两种形状(一个有 `+00:00` 一个没有)。前端两种都认,所以界面上看不出来
    —— 这正是它能一直存在的原因;而导出文件和任何直接消费这些 JSON 的
    第三方拿到的是不一致的格式,只会在对账那天才发现。

    这条是**面积型**问题:改完之后必须有个东西拦住下一处,否则下一个
    `.isoformat()` 明天就会长出来。
    """
    offenders: list[str] = []
    for folder in ("api", "services"):
        for path in sorted((APP / folder).glob("*.py")):
            for lineno, line in enumerate(_read(path).splitlines(), 1):
                if ".isoformat()" not in line or _DATE_KEY.search(line):
                    continue
                offenders.append(f"{folder}/{path.name}:{lineno}")
    assert not offenders, (
        "裸 `.isoformat()` 又出现了,同一个时间字段会有两种格式 -> "
        + "; ".join(offenders)
    )


def test_the_date_key_exception_is_still_a_date_key():
    """上面那条的豁免必须窄:只豁免日期键,不是豁免整个文件。"""
    src = _read(APP / "services" / "spend.py")
    exempt = [line.strip() for line in src.splitlines() if _DATE_KEY.search(line)]
    assert exempt, "日期键不见了 —— 豁免规则的前提没了,应当收掉它"
    for line in exempt:
        assert "key = " in line, f"豁免命中了一个不是日期键的地方:{line}"


# ==========================================================
# A-32:列表端点不再逐批次全查条目
# ==========================================================


def test_the_batch_list_fetches_items_once():
    """A-32:30 个批次不该打 30 次条目查询。

    计数与活性判定确实需要每一行(`unexplained` 要看每行的 `error_code`,
    心跳要看 `finished_at` 的最大值),所以省不掉行 —— 能省的是**往返次数**。
    办法与 A-08 修发布清单时完全一样:按页一次取回,再在内存里分发。
    """
    service = APP / "workbench" / "batch_service.py"
    grouped = ast.unparse(_fn(service, "_items_by_batch"))
    assert "in_(" in grouped, "批量取条目没有用 IN"

    jobs_out = ast.unparse(_fn(service, "jobs_out"))
    assert "_items_by_batch" in jobs_out and "rows=" in jobs_out, (
        "列表出参没有把预取的行传下去 —— 传不下去等于白取"
    )

    api = _read(APP / "api" / "workbench_batch.py")
    assert "bs.jobs_out(session, jobs)" in api, "列表端点又逐批次调 job_out 了(A-32)"
    assert "[bs.job_out(session, job) for job in jobs]" not in api

    # 判定不许跟着改:仍然是同一个 job_out
    assert "def job_out(" in _read(service)
    assert "count_rows" in _read(service), "计数口径必须仍然只有一份"


def test_the_two_item_queries_stay_in_the_same_order():
    """详情与列表取条目的排序必须一致。

    不一致的话"第一件是哪件"在两个页面上会不一样,而两边都不报错。
    """
    service = _read(APP / "workbench" / "batch_service.py")
    orders = re.findall(
        r"order_by\(\s*BatchJobItem\.created_at,\s*BatchJobItem\.sku\s*\)", service
    )
    assert len(orders) >= 2, (
        f"只找到 {len(orders)} 处条目排序,预期至少 2 处(_items 与 _items_by_batch)"
    )


# ==========================================================
# C-07:配置审计记改前后
# ==========================================================


def test_the_settings_audit_records_the_old_and_new_value():
    """C-07:只记键名回答不了"他把它改成了什么"。

    排查一次配置事故(模型突然切了、额度突然烧完)问的正是后者。
    """
    path = APP / "services" / "settings_service.py"
    src = _src(path, "apply")
    assert '"changes"' in src and "diff" in src, "配置审计仍然只记键名(C-07)"
    assert '"before"' in src and '"after"' in src, "没有改前 / 改后两侧"


def test_secrets_never_reach_the_audit_log_in_clear_text():
    """敏感项只进掩码。审计表不能变成第二个明文口令仓库。

    密文同样不行 —— 密钥轮换之后那串东西谁也解不开,而审计要的是
    "当时是不是那一把",不是"当时那一把具体是什么"。
    """
    path = APP / "services" / "settings_service.py"
    auditable = _src(path, "_auditable")
    assert "spec.secret" in auditable and "value_hint" in auditable, (
        "改前值没有按敏感性分流"
    )
    assert "decrypt" not in auditable, "审计里出现了解密调用"

    src = _src(path, "apply")
    assert '"after": hint if spec.secret else value' in src, (
        "改后值没有按敏感性分流 —— 明文口令会进审计"
    )
    assert '"after": stored' not in src, "把密文写进审计了"


# ==========================================================
# C-12:生产误用本地存储要当场炸
# ==========================================================


def test_local_storage_is_refused_in_production():
    """C-12:安静地跑起来才是最坏的结果。

    本地存储把字节写进容器里的一个目录。生产上重建一次容器,所有素材和
    导出文件全部消失,而库里的引用还在 —— 表现是界面正常、图片全裂,
    并且是在重建之后才出现,没有人会把它和存储配置联系起来。
    """
    src = ast.unparse(_fn(APP / "services" / "storage.py", "build_storage"))
    assert "is_production" in src, "生产环境用本地存储仍然能安静跑起来(C-12)"
    assert "ConfigError" in src, "拒绝方式不是一个配置错误"
    # 判据只认明确的 production/prod,与 settings 那边同源
    config = _read(APP / "core" / "config.py")
    assert '{"production", "prod"}' in config, (
        "生产判据变了 —— 两处必须同源,否则存储守卫会和别的守卫说两句话"
    )


# ==========================================================
# C-15:实现与承诺同向
# ==========================================================


def test_a_structured_error_code_beats_a_message_fragment():
    """C-15:注释说 `code` 优先,实现原来先扫消息片段 —— 正好相反。

    评审查过当时的 9 个码与 9 条 hint,结论是"今天没有任何一条会被误分类",
    所以它是潜在风险不是现行缺陷。但"今天不冲突"是会过期的结论:下一个人
    加 hint 时读的是注释,而注释说 `code` 优先 —— 他会假设自己的 hint 只在
    没有 `code` 时生效,实际上它会盖掉一个结构化错误码,**两处都不报错**。
    """
    from app.workbench import batch as rules

    fragment, hinted = rules.MESSAGE_HINTS[0]
    raw_code, mapped = next(iter(rules.ERROR_CODE_MAP.items()))

    class _Both(Exception):
        code = raw_code

        def __str__(self) -> str:
            return f"包含片段:{fragment}"

    got, _ = rules.classify_exception(_Both())
    assert got is mapped, (
        f"同时有 code 和消息片段时选了 {got} —— 结构化的那个应当赢(C-15)"
    )

    # 没有 code 时消息片段仍然是兜底,不能一起改没了
    got2, _ = rules.classify_exception(Exception(f"只有片段:{fragment}"))
    assert got2 is hinted, "消息片段兜底失效了 —— 这条修复不该顺手关掉兜底"

    # 都不命中才落 UNEXPLAINED
    got3, _ = rules.classify_exception(Exception("完全认不出来的一句话"))
    assert got3 is rules.BatchErrorCode.UNEXPLAINED


# ==========================================================
# 第七批自查发现的:落地了但没人可达
# ==========================================================


def test_every_repair_tool_has_a_way_to_be_run():
    """写好的修复函数必须有入口。**这是本轮自查抓到的一条。**

    `purge_superseded_exports()`（C-17）第六批就写完了,自查 `grep` 时
    **0 处引用** —— 一个实现完整、无人可达的函数,和没做是一样的。

    而这恰恰是评审里 A-21 与 A-26 的形状:409 提示"请重新生成"而全仓没有
    重新生成的入口;覆盖横幅说"绑定了 0 个"而界面上无处可点。两条都是我
    判为缺陷的,然后自己又造了一个。

    所以这条门禁盯的不是某一个函数,是**这一类**:运维脚本必须同时有
    模块入口和 Makefile 目标,否则它只是一段注释。
    """
    makefile = (BACKEND_ROOT.parent / "Makefile").read_text(encoding="utf-8")
    for tool, target in (
        ("purge_exports", "purge-exports"),
        ("repair_variant_owners", "repair-variant-owners"),
    ):
        path = BACKEND_ROOT / "tools" / f"{tool}.py"
        assert path.exists(), f"tools/{tool}.py 不见了"
        source = _read(path)
        assert "def main(" in source, f"{tool} 没有 main() —— `python -m` 跑不起来"
        assert '__name__ == "__main__"' in source, f"{tool} 不能被 `python -m` 执行"
        assert '"--apply"' in source, f"{tool} 没有 --apply,也就无从默认干跑"
        assert f"{target}:" in makefile, (
            f"{tool} 没有 Makefile 入口 —— 没人会发现它存在(C-17 就是这么漏掉的)"
        )
        assert f"tools.{tool}" in makefile, f"{target} 目标没有真的调到 {tool}"


def test_the_export_reclaimer_is_actually_reachable():
    """C-17 的实现与入口必须对得上。

    入口调错函数名、或者调了一个签名不同的版本,`grep` 仍然是有引用的 ——
    所以这条钉的是**调用点真的指向那个函数**,不是"文件里出现过这个词"。
    """
    entry = ast.parse(_read(BACKEND_ROOT / "tools" / "purge_exports.py"))
    calls = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "purge_superseded_exports"
    ]
    assert calls, "入口没有调到 purge_superseded_exports()"
    passed = {kw.arg for kw in calls[0].keywords}
    assert {"older_than_days", "apply"} <= passed, (
        f"入口没把年龄与干跑两个参数传下去,只传了 {sorted(passed)}"
    )
