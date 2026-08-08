#!/usr/bin/env python3
"""A45-batch14 变异验证:把每条守卫对应的决定改回去,确认它真的变红。

用法:python3 tools/mutate_batch14.py

一条永远不会红的守卫比没有守卫更糟 —— 它看起来在保护。
batch13 那一轮 16 条变异里有 4 条第一版是绿的,而 4 条全都是
"守卫在验错的东西"(比对注释、比对相邻约束、比对文档字符串里的路径)。
所以这份脚本逐条替换**源码**,而不是替换测试。

## 它刻意不进 CI(与 `tools/p0_gate.py` 顶部同一条理由)

跑一次要复制 34 份工作树、改 34 次源码、跑 34 遍套件。放进每次 push 的门禁里,
它会因为耗时被人加 `|| true`,然后变成一条恒绿的装饰。

它的读者是**下一个改这些判定的人**:改完之后手动跑一次,确认守卫仍然咬得住。
新增判定时要同时往 `MUTATIONS` 里加一条 —— 加一条守卫而不加变异,
等于没有人验证过那条守卫会不会红。

## 输出里那一列失败用例名是必需的

第一版只报红/绿,于是「32/32 全红」看起来完美,而实际上其中三条的红
来自一个完全无关的原因(沙箱没复制项目根,一条读 `.env.example` 的守卫
在每次变异里都红)。**只看红绿的变异验证会把假红当成证据。**
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

#: (编号, 一句话, 文件, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1",
        "目标过滤退回按注册表判(增量识别时多答的字段会落证据)",
        "app/extractors/evidence_plan.py",
        "        if name not in wanted:\n            fabricated_names.append(name[:64])\n            continue\n        if name in no_value:",
        "        if False:\n            fabricated_names.append(name[:64])\n            continue\n        if name in no_value:",
    ),
    (
        "M2",
        "编造只写日志不计数",
        "app/extractors/evidence_plan.py",
        "        fabricated=len(fabricated_names),",
        "        fabricated=0,",
    ),
    (
        "M3",
        "模型说看不清时不落证据(退回 batch14 之前的口径)",
        "app/extractors/evidence_plan.py",
        "        evidence.append(PlannedEvidence(field_name=name, missing_reason=reason))",
        "        pass",
    ),
    (
        "M4",
        "认不出来的 reason 直接丢掉整条证据,而不是降级",
        "app/extractors/evidence_plan.py",
        "        no_value[name] = reason if reason in ALLOWED_REASONS else REASON_INSUFFICIENT_EVIDENCE",
        "        if reason in ALLOWED_REASONS:\n            no_value[name] = reason",
    ),
    (
        "M5",
        "对没问过的字段宣布没有值也落证据(编造过滤的绕行缝)",
        "app/extractors/evidence_plan.py",
        "    for name, reason in no_value.items():\n        if name not in wanted:",
        "    for name, reason in no_value.items():\n        if False:",
    ),
    (
        "M6",
        "既给值又说看不清时让值胜出",
        "app/extractors/evidence_plan.py",
        "        if name in no_value:\n            # 规则 4:模型自己说这个字段判断不了,却又给了一个值\n            fabricated_names.append(name[:64])\n            continue",
        "        if False:\n            fabricated_names.append(name[:64])\n            continue",
    ),
    (
        "M7",
        "编造字段名不截断,原样进结果",
        "app/extractors/evidence_plan.py",
        "            fabricated_names.append(name[:64])\n            continue\n        if name in no_value:",
        "            fabricated_names.append(name)\n            continue\n        if name in no_value:",
    ),
    (
        # **这条原来是假红**(A45-batch14-5 修)。
        #
        # 老写法把调用换成 `_EvidencePlanShim(result, targets)` —— 一个
        # 谁都没定义的名字。跑起来当场 NameError,而脚本按 `rc != 0` 判"响了",
        # 于是它 17 轮都报红,红的却是**模块炸了**,不是守卫咬住了。
        # `crashed` 那道闸事后能标出来,但标出来之后没有人回来修:
        # 结果是 `test_the_service_materialises_the_plan_instead_of_deciding_again`
        # 从来没有被任何一条变异验证过。
        #
        # 这与 F7 是同一句话的第三次出现:**红是真的,红的原因不是。**
        #
        # 新写法是语法合法、import 得动、跑得起来的 —— 服务层就地自己
        # 拼一份计划,不再走纯判定。这才是那条守卫要拦的东西,而且它同时
        # 踩中两条断言(没调 `plan_evidence`、服务层重新长出目标过滤)。
        "M8",
        "服务层不再调纯判定,自己判一遍",
        "app/attributes/service.py",
        "        plan = plan_evidence(result, targets)",
        "        from app.extractors.evidence_plan import EvidencePlan\n"
        "\n"
        "        target_set = {getattr(t, 'name', t) for t in targets}\n"
        "        plan = EvidencePlan(\n"
        "            evidence=tuple(f for f in result.fields if f.name not in target_set),\n"
        "        )",
    ),
    (
        "M9",
        "付费上限整个删掉(一次点击 = 一整批付费调用)",
        "app/extractors/call_budget.py",
        "    if not billable:\n        return False",
        "    if not billable:\n        return False\n    return False",
    ),
    (
        "M10",
        "调用失败的那一路不记流水",
        "app/attributes/service.py",
        "            if billable:\n                _record_extraction_usage(\n                    session,\n                    extractor=extractor,\n                    succeeded=False,",
        "            if False:\n                _record_extraction_usage(\n                    session,\n                    extractor=extractor,\n                    succeeded=False,",
    ),
    (
        "M11",
        "识别流水混进生成的 submit",
        "app/attributes/service.py",
        'operation="attribute_extract",',
        'operation="submit",',
    ),
    (
        "M12",
        "识别流水传 billing_key(第二张图会撞唯一约束)",
        "app/attributes/service.py",
        "        provider_attempts=reported_attempts,\n    )",
        '        provider_attempts=reported_attempts,\n        billing_key="extract",\n    )',
    ),
    (
        "M13",
        "注册表的 configured 退回按名单填",
        "app/extractors/registry.py",
        "            configured = bool(instance.is_configured())",
        "            instance = instance\n            configured = True",
    ),
    (
        "M14",
        "注册表的 is_simulator 退回写死 True",
        "app/extractors/registry.py",
        '"is_simulator": bool(getattr(factory, "is_simulator", True)),',
        '"is_simulator": True,',
    ),
    (
        "M15",
        "某个后端炸掉时让状态接口跟着炸",
        "app/extractors/registry.py",
        "        except Exception:  # noqa: BLE001 - 状态上报路径,失败即\"没配好\"\n            configured = False",
        "        except ZeroDivisionError:\n            configured = False",
    ),
    (
        "M16",
        "识别配置回退到评分器的键",
        "app/extractors/vision.py",
        'provider_setting("EXTRACTOR_MODEL_BASE_URL")',
        'provider_setting("VISION_MODEL_BASE_URL")',
    ),
    (
        "M17",
        "要不要 Key 的判定自己再写一份(A45-#36 复发)",
        "app/extractors/vision.py",
        "        return endpoint_trust.endpoint_requires_api_key(\n            self.base_url, allow_anonymous=self.allow_anonymous\n        )",
        "        from urllib.parse import urlparse\n\n        host = (urlparse(self.base_url).hostname or \"\").lower()\n        return not host.startswith((\"10.\", \"192.168.\", \"127.\"))",
    ),
    (
        "M18",
        "厂商名自己再推一份(与评分器分列两行账)",
        "app/extractors/vision.py",
        "        return endpoint_trust.provider_label_from_base_url(\n            str(self.config.base_url or \"\"), fallback=self.name\n        )",
        "        return self.name",
    ),
    (
        "M19",
        "空响应/解析失败时返回空结果而不是抛错",
        "app/extractors/schema.py",
        '        raise ExtractionParseError(\n            "返回内容不是合法 JSON", detail={"reason": str(exc)}\n        ) from exc',
        "        return {}",
    ),
    (
        "M20",
        "置信度越界时夹到区间内而不是拒绝",
        "app/extractors/schema.py",
        '                raise ExtractionParseError(\n                    f"fields[{index}].confidence 越界:{confidence}",\n                    detail={"field": name.strip()[:64]},\n                )',
        "                confidence = min(1.0, max(0.0, float(confidence)))",
    ),
    (
        "M21",
        "解析层顺手把不认识的字段丢掉(编造从此数不出来)",
        "app/extractors/schema.py",
        "        observations.append(\n            FieldObservation(",
        "        if name.strip() not in REGISTRY:\n            continue\n        observations.append(\n            FieldObservation(",
    ),
    (
        "M22",
        "模型可以自报 AWAITING_SUPPLIER_DATA",
        "app/extractors/schema.py",
        "MODEL_MISSING_REASONS: tuple[str, ...] = (\n    MissingReason.INSUFFICIENT_EVIDENCE.value,\n    MissingReason.NOT_VISUALLY_DETERMINABLE.value,\n)",
        "MODEL_MISSING_REASONS: tuple[str, ...] = tuple(m.value for m in MissingReason)",
    ),
    (
        "M23",
        "发给厂商的 Schema 带上 _enum_hints",
        "app/extractors/schema.py",
        '    out = {k: copy.deepcopy(v) for k, v in schema.items() if k != "_enum_hints"}',
        "    out = {k: copy.deepcopy(v) for k, v in schema.items()}",
    ),
    (
        "M24",
        "strict 模式下 evidence 不进 required",
        "app/extractors/schema.py",
        '        if "evidence" not in required:\n            required.append("evidence")',
        "        pass",
    ),
    (
        "M25",
        "api_ready_schema 就地改输入",
        "app/extractors/schema.py",
        "    out = {k: copy.deepcopy(v) for k, v in schema.items() if k != \"_enum_hints\"}",
        "    out = {k: v for k, v in schema.items() if k != \"_enum_hints\"}",
    ),
    (
        "M26",
        "推理模式下仍然发 temperature(换来一个不重试的 400)",
        "app/extractors/vision.py",
        '        effort = self.config.reasoning_effort\n        if effort and effort != "none":',
        '        effort = self.config.reasoning_effort\n        body["temperature"] = 0\n        if effort and effort != "none":',
    ),
    (
        "M27",
        "降级到 json_object 时仍然发 Schema",
        "app/extractors/vision.py",
        '        if self.config.response_format == FORMAT_JSON_SCHEMA:\n            body["text"] = {\n                "format": {\n                    "type": "json_schema",',
        '        if self.config.response_format in (FORMAT_JSON_SCHEMA, FORMAT_JSON_OBJECT):\n            body["text"] = {\n                "format": {\n                    "type": "json_schema",',
    ),
    (
        "M28",
        "把 API Key 也塞进请求体",
        "app/extractors/vision.py",
        '            "stream": False,\n            "store": False,\n        }',
        '            "stream": False,\n            "store": False,\n            "api_key": self.config.api_key,\n        }',
    ),
    (
        "M29",
        "迁移的降级漏撤一列",
        "migrations/versions/0036_extraction_missing_reason.py",
        '    op.drop_column("attribute_evidence", "missing_reason")',
        "    pass",
    ),
    (
        "M33",
        # A45-batch14-2 / F3 把兜底抽成了 `effective_ceiling()`,锚点跟着搬 ——
        # 变异要打的仍然是同一个决定:非正数上限当作"不限"
        "上限判定把 0 当成不限",
        "app/extractors/call_budget.py",
        "    return ceiling if ceiling > 0 else DEFAULT_MAX_IMAGES_PER_RUN",
        "    return ceiling",
    ),
    (
        "M34",
        "边界写成 >= (正好等于上限时也拒绝)",
        "app/extractors/call_budget.py",
        "    return image_count > effective_ceiling(ceiling)",
        "    return image_count >= effective_ceiling(ceiling)",
    ),
    (
        "M30",
        "missing_reason 给一个 server_default(替旧证据宣布它们说过看不清)",
        "migrations/versions/0036_extraction_missing_reason.py",
        'sa.Column("missing_reason", sa.String(length=32), nullable=True),',
        'sa.Column(\n            "missing_reason",\n            sa.String(length=32),\n            nullable=True,\n            server_default="INSUFFICIENT_EVIDENCE",\n        ),',
    ),
    (
        "M31",
        "evidence_plan 的 reason 常量与枚举漂移",
        "app/extractors/evidence_plan.py",
        'REASON_AWAITING_SUPPLIER_DATA = "AWAITING_SUPPLIER_DATA"',
        'REASON_AWAITING_SUPPLIER_DATA = "AWAITING_SUPPLIER"',
    ),
    (
        "M32",
        "识别层直接用 httpx,绕过退避与错误归类",
        "app/extractors/vision.py",
        "import time\nfrom collections.abc import Mapping",
        "import time\n\nimport httpx  # noqa: F401\nfrom collections.abc import Mapping",
    ),
]


#: 只跑本批的守卫文件。跑全量每次 25 秒 × 32 条 = 13 分钟,而本批的变异
#: **不应该**让别的文件变红 —— 真让别处红了,那是回归,由 `--full` 兜底。
SUITE_FILTER = "test_a45_batch14_stage3_extractor.py"


def run_suite(cwd: Path, name_filter: str = SUITE_FILTER) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    results: list[tuple[str, str, bool]] = []
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
            )
            work = root / "backend"
            target = work / rel
            text = target.read_text(encoding="utf-8")
            if old not in text:
                # 元组必须和下面那条同宽(A45-batch14-2)。原来这里只塞三项,
                # 而汇总循环按五项解包 —— 于是**锚点失效时脚本自己 ValueError**,
                # 报出来的是一句 traceback,不是"M33 的锚点过期了"。
                #
                # 锚点失效恰恰是最常发生的一件事:任何人重构被变异的那段源码
                # 都会让它过期(F3 就让 M33/M34 同时失锚)。一个"在最常见的
                # 失败路径上自己崩掉"的工具,和恒绿的守卫是同一类问题。
                results.append(
                    (ident, f"{description} —— 锚点找不到,变异没生效", False, [], False)
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            crashed = any(
                marker in output
                for marker in ("NameError", "ImportError", "SyntaxError", "ModuleNotFoundError")
            )
            results.append((ident, description, code != 0, failed, crashed))

    ok = True
    for ident, description, went_red, failed, crashed in results:
        mark = "RED " if went_red else "GREEN"
        note = ""
        if crashed:
            # 变异把模块炸了就不算数:那时候**每条**用例都会红,
            # 证明不了任何一条守卫在工作 —— 与 batch13 那 4 条假红同一个道理
            note = "  <- 导入期就炸了,这条变异不算数"
            ok = False
        elif went_red:
            note = "  " + ", ".join(failed[:2]) if failed else ""
        print(f"{ident:<4} {description}  {mark}{note}")
        ok = ok and went_red
    print()
    reds = sum(1 for r in results if r[2] and not r[4])
    print(f"{reds}/{len(results)} 条变异被守卫抓住(且不是靠炸掉模块)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
