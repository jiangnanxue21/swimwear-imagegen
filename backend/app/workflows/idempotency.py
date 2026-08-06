"""幂等键推导。需求第九章:使用幂等键防止重复任务。

规则:
- 调用方显式传入的键优先,原样使用(截断到 128 字符);
- 未传时由「决定生成结果的输入」派生。判定标准只有一条:
  **改了它,出来的图会不一样吗?** 会,就必须进哈希。
- **轮次不参与推导**:重生是同一个任务的后续轮次,不是新任务。

早期版本只放了商品、素材、模板、模式、Provider、候选数和 prompt 版本,
于是改了提示词、输出尺寸、起始 seed 或 provider_params 再提交,会静默命中旧任务 ——
用户看到「提交成功」,拿到的却是上一版参数生成的图,而且没有任何提示。
现在这些全部进哈希;`prompt_version` 保留下来,它仍然是提示词模板迭代的显式开关。

注意 base_seed 与轮次 seed 的区别:base_seed 是**输入**,换了它整条轨迹都不同,
所以要进哈希;每轮派生的 seed 是同一任务的内部演进,不进。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


#: 幂等键的长度上限。**与 `generation_tasks.idempotency_key` 的列宽同源。**
#:
#: 迁移 0041 把列从 128 扩到 256。这个常量原来是写死在 `[:128]` 里的字面量,
#: 而截断是幂等这件事最坏的失效方式:两份不同的显式键被截成同一个,
#: 第二次提交会命中第一次的任务,**而两次的参数不同**。
#: 列宽和截断长度必须一起改,所以它们现在有一个共同的名字。
MAX_IDEMPOTENCY_KEY_CHARS = 256


def build_idempotency_key(
    *,
    product_id: str,
    mode: str,
    provider: str,
    asset_ids: list[str] | None = None,
    model_template_id: str | None = None,
    candidate_count: int = 4,
    prompt_version: str = "v1",
    routing_mode: str = "",
    max_rounds: int = 0,
    output_width: int | None = None,
    output_height: int | None = None,
    base_seed: int | None = None,
    prompt: str | None = None,
    negative_prompt: str | None = None,
    provider_params: dict[str, Any] | None = None,
    color_variant_id: str | None = None,
    plan_fingerprint: str | None = None,
    sample_fingerprint: str | None = None,
    explicit_key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()[:MAX_IDEMPOTENCY_KEY_CHARS]

    payload = {
        "product_id": str(product_id),
        "mode": str(mode),
        "provider": str(provider),
        "assets": sorted(str(a) for a in (asset_ids or [])),
        "model_template_id": str(model_template_id or ""),
        "candidate_count": int(candidate_count),
        "prompt_version": str(prompt_version),
        "routing_mode": str(routing_mode or ""),
        "max_rounds": int(max_rounds or 0),
        "output_width": int(output_width) if output_width else 0,
        "output_height": int(output_height) if output_height else 0,
        "base_seed": int(base_seed) if base_seed is not None else None,
        "prompt": (prompt or "").strip(),
        "negative_prompt": (negative_prompt or "").strip(),
        "provider_params": _canonical(provider_params or {}),
        # ---- PRD v3.1 阶段 4:颜色作用域与方案(§6.5 / §9.3)----
        #
        # 三个都进键,而且三个都是**独立**的输入,不能只留一个:
        #
        #   color_variant_id    同一个 SKU 行可以属于不同颜色的任务(多色 SPU 的
        #                       任务今天只挂 product_id,而一个颜色有多个尺码)
        #   plan_fingerprint    改了角度/模特/预算再提交必须是一次新尝试。
        #                       少了它,§6.4 的"确认方案"按钮点了等于没点
        #   sample_fingerprint  换了样品图之后重出,是新任务不是旧任务 ——
        #                       §8.1 第 2 行的"图片结果过期"要能真的重出一批
        #
        # 都用空串而不是 None 兜底:`json.dumps` 会把 None 编成 `null`,
        # 而"没传"和"传了空"在这里必须是同一个键,否则接口两条调用路径
        # (前端不传 / 服务层算出空)会给出两个不同的任务
        "color_variant_id": str(color_variant_id or ""),
        "plan_fingerprint": str(plan_fingerprint or ""),
        "sample_fingerprint": str(sample_fingerprint or ""),
        "extra": extra or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "auto-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]


def _canonical(value: Any) -> Any:
    """把嵌套结构归一成对键序不敏感的形式。

    provider_params 是自由字典,{"a":1,"b":2} 与 {"b":2,"a":1} 是同一份参数,
    必须算出同一个键,否则幂等会形同虚设。
    """
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


#: seed 的取值上界。generation_tasks.base_seed / generation_attempts.seed 都是
#: PostgreSQL INTEGER(有符号 32 位),超过 2**31-1 会在 INSERT 时直接
#: NumericValueOutOfRange。取 4 字节摘要有一半概率越界,必须掩掉符号位。
MAX_SEED = 2**31 - 1


def next_seed(base_seed: int, round_number: int) -> int:
    """每轮换新 seed(需求第九章),但必须可复现 —— 同一任务同一轮永远同一个 seed。"""
    digest = hashlib.sha256(f"{base_seed}:{round_number}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & MAX_SEED
