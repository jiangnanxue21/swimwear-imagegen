"""生成链路冒烟测试(需求第二十一章阶段 7)。

对着**跑起来的系统**走一遍生成链路,每步打勾或打叉:

    健康检查 -> 建商品 -> 传素材 -> 建已授权模特模板 -> 建任务
    -> 等生成与评分 -> 分档决策 -> 多尺寸输出 -> 导出 URL 与 CSV -> 仪表盘

## 覆盖边界(A45 独立审查 C-11:这份脚本以前自称"完整闭环",它不是)

**不覆盖**:人工审核、图片集批准、文案生成与批准、上架草稿、发布提交、
平台轮询、驳回与下架。审核之后的链路(6.5–6.10)由真库 pytest
(`test_publish_flow_db.py` / `test_poll_and_delist_db.py` 等)做集成验证,
交互路径仍需按 `LOCAL_MANUAL_TEST.md` 手工走 —— 这份脚本跑绿**不构成**
发布闭环可用的证据,只构成生成链路可用的证据。

模特走**已授权模板**而不是 MODEL_REFERENCE 素材:后者是 STATUS.md 里记着的
合规绕行缝(跳过 §10.5/§11 全部检查),冒烟走它等于授权闸一次都没被执行过。

和 pytest 的分工:pytest 验证"每个零件对不对",这个脚本回答
"现在这套系统能不能用"。部署完、改完配置、升级完镜像,跑它一次比读日志快。

用法::

    docker compose exec backend python -m app.scripts.smoke_test
    docker compose exec backend python -m app.scripts.smoke_test --provider fashn --keep

默认用 mock Provider 与 mock 评分器,**不产生任何外部费用**。
默认跑完清理自己造的数据,加 --keep 保留下来供人工查看。
"""
from __future__ import annotations

import argparse
import io
import sys
import time
import uuid
from typing import Any

BASE_URL_DEFAULT = "http://localhost:8000"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

#: 跑完就不会再变的状态
TERMINAL = {
    "COMPLETED", "FAILED", "CANCELLED",
    "MANUAL_REVIEW", "AUTO_REJECTED", "MANUALLY_REJECTED",
}


class Smoke:
    def __init__(self, base_url: str, timeout: float) -> None:
        import httpx

        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.failures: list[str] = []
        self.step = 0

    # ---------- 输出 ----------

    def ok(self, label: str, detail: str = "") -> None:
        self.step += 1
        print(f"  {GREEN}✓{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        self.step += 1
        self.failures.append(label)
        print(f"  {RED}✗{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))

    def note(self, text: str) -> None:
        print(f"    {DIM}{text}{RESET}")

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        (self.ok if condition else self.fail)(label, detail)
        return condition

    # ---------- 步骤 ----------

    def health(self) -> bool:
        try:
            live = self.client.get("/api/health")
            ready = self.client.get("/api/health/ready")
        except Exception as exc:  # noqa: BLE001
            self.fail("后端可达", f"{type(exc).__name__}:后端没起来?")
            return False

        self.check("存活探针", live.status_code == 200)
        body = ready.json() if ready.status_code < 500 else {}
        healthy = ready.status_code == 200
        self.check("就绪探针(DB / Redis / 存储)", healthy, str(body.get("status", "")))
        if not healthy:
            for name, info in (body.get("checks") or {}).items():
                self.note(f"{name}: {info}")
        return healthy

    def create_product(self, tag: str) -> str | None:
        payload = {
            "spu": f"SMOKE-{tag}",
            "sku": f"SMOKE-{tag}-S",
            "name": "冒烟测试泳衣",
            # 受众显式给出:§10.5 的模特受众硬约束、C-03 的受众×品类组合
            # 校验都要有受众才判得动 —— 冒烟应当路过这些闸,不是绕开它们
            "audience": "WOMEN",
            "garment_type": "BIKINI_SET",
            "primary_color": "BLACK",
        }
        response = self.client.post("/api/products", json=payload)
        if not self.check("创建商品", response.status_code == 201, response.text[:120]):
            return None
        return response.json()["id"]

    def upload_assets(self, product_id: str) -> bool:
        from PIL import Image

        def image_bytes(color, size=(900, 1200)) -> bytes:
            buffer = io.BytesIO()
            Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()

        uploads = [
            ("GARMENT_FRONT", "front.jpg", image_bytes((25, 30, 40))),
            ("MODEL_REFERENCE", "model.jpg", image_bytes((210, 180, 150))),
        ]
        for asset_type, name, data in uploads:
            response = self.client.post(
                f"/api/products/{product_id}/assets",
                files={"file": (name, data, "image/jpeg")},
                data={"asset_type": asset_type},
            )
            if not self.check(f"上传素材 {asset_type}", response.status_code == 201,
                              response.text[:120]):
                return False
        return True

    def create_model_template(self, tag: str) -> str | None:
        """建一个**已授权、已确认成年**的模特模板。

        以前这里不传 model_template_id,任务走的是 MODEL_REFERENCE 素材那条
        已知的合规绕行缝(STATUS.md 已知缺口):§10.5 受众、§11 授权/年龄/
        AI 换装四道检查在这份冒烟里**一次都没有被执行过** —— 门禁绿着,
        闸门却从没合上过。改走已授权模板之后,这四道每次冒烟都真的会过一遍。
        """
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (900, 1200), (200, 170, 140)).save(
            buffer, format="JPEG", quality=90
        )
        response = self.client.post(
            "/api/model-templates",
            files={"file": (f"smoke-{tag}-model.jpg", buffer.getvalue(), "image/jpeg")},
            data={
                "name": f"SMOKE-{tag} 模特",
                "audience": "WOMEN",  # 与冒烟商品一致(§10.5 硬过滤)
                "license_status": "LICENSED",
                "age_verified": "true",
                "allow_ai_dressing": "true",
            },
        )
        if not self.check(
            "创建已授权模特模板",
            response.status_code in (200, 201),
            response.text[:120],
        ):
            return None
        return response.json()["id"]

    def create_task(
        self, product_id: str, provider: str, model_template_id: str
    ) -> str | None:
        payload = {
            "product_id": product_id,
            "mode": "virtual_try_on",
            "provider": provider,
            # 显式选已授权模特:让 §10.5/§11 的生成前硬阻断真的在冒烟里执行
            "model_template_id": model_template_id,
            "candidate_count": 4,
            "max_rounds": 1,
            "provider_params": {"mock_evaluator": {"outcome": "a"}},
        }
        started = time.monotonic()
        response = self.client.post("/api/generation-tasks", json=payload)
        elapsed = (time.monotonic() - started) * 1000

        if not self.check("创建生成任务", response.status_code == 201, response.text[:120]):
            return None
        # 需求第十六章:创建接口必须立即返回,不等生成完成
        self.check("任务创建立即返回", elapsed < 3000, f"{elapsed:.0f}ms")
        return response.json()["task"]["id"]

    def wait_for_task(self, task_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/generation-tasks/{task_id}").json()
            status = detail.get("status", "")
            if status != last:
                self.note(f"状态 {status}")
                last = status
            if status in TERMINAL:
                return detail
            time.sleep(1.5)
        self.fail("任务在超时内跑完", f"仍停在 {last};worker 起来了吗?")
        return None

    def check_pipeline(self, detail: dict[str, Any]) -> bool:
        status = detail.get("status")
        if not self.check("任务跑到完成", status == "COMPLETED", f"实际 {status}"):
            if detail.get("error_code"):
                self.note(f"{detail['error_code']}: {detail.get('error_message')}")
            return False

        candidates = detail.get("candidates") or []
        self.check("每轮产出多张候选图", len(candidates) >= 4, f"{len(candidates)} 张")
        self.check(
            "候选图全部落到自有存储",
            all(c.get("url") for c in candidates),
            "第三方 URL 必须尽快下载到自己的存储",
        )
        scored = [c for c in candidates if c.get("overall_score") is not None]
        self.check("候选图完成评分", len(scored) >= 1, f"{len(scored)} 张有分")
        selected = [c for c in candidates if c.get("status") == "SELECTED"]
        self.check("只选中一张", len(selected) == 1, f"{len(selected)} 张")
        return True

    def check_evaluations(self, task_id: str) -> None:
        rows = self.client.get(f"/api/generation-tasks/{task_id}/evaluations").json()
        self.check("评分结果是结构化数据", bool(rows) and isinstance(rows[0].get("scores"), dict))
        if rows:
            grades = {r.get("grade") for r in rows}
            self.check("每张候选图都有分档", None not in grades, str(sorted(grades)))

    def check_outputs(self, product_id: str) -> None:
        body = self.client.get(f"/api/exports/products/{product_id}").json()
        images = body.get("images") or {}
        expected = {"ORIGINAL", "DETAIL", "THUMBNAIL", "MOBILE", "SQUARE"}
        self.check("产出五个尺寸的网站图", set(images) == expected, f"{sorted(images)}")

        square = images.get("SQUARE")
        if square:
            self.check("正方形版本确实是正方形",
                       square["width"] == square["height"],
                       f"{square['width']}x{square['height']}")

        reachable = 0
        for image in images.values():
            try:
                head = self.client.get(image["url"])
                reachable += 1 if head.status_code == 200 else 0
            except Exception:  # noqa: BLE001
                pass
        self.check("成品图 URL 可访问", reachable == len(images), f"{reachable}/{len(images)}")

    def check_export_csv(self, product_id: str) -> None:
        response = self.client.get(
            f"/api/exports/products/{product_id}", params={"format": "csv"}
        )
        ok = response.status_code == 200 and "csv" in response.headers.get("content-type", "")
        self.check("CSV 导出", ok, response.headers.get("content-type", ""))
        if ok:
            lines = [line for line in response.text.splitlines() if line.strip()]
            self.check("CSV 一个 SKU 一行", len(lines) == 2, f"{len(lines)} 行(含表头)")

    def check_dashboard(self) -> None:
        body = self.client.get("/api/dashboard/summary").json()
        self.check("仪表盘可用", "products" in body and "tasks" in body)
        self.check("仪表盘统计到成品图", (body.get("outputs") or {}).get("total", 0) >= 5,
                   str((body.get("outputs") or {}).get("total")))

    def check_providers(self) -> None:
        response = self.client.get("/api/providers")
        self.check("Provider 列表可用", response.status_code == 200)
        blob = response.text.lower()
        self.check("接口不泄露密钥", "api_key" not in blob and "secret" not in blob)

    def cleanup(self, product_id: str, template_id: str | None) -> None:
        # 没有删除接口(MVP 不做删除),改为归档,避免污染统计
        response = self.client.patch(f"/api/products/{product_id}", json={"status": "ARCHIVED"})
        if response.status_code == 200:
            self.note(f"已把测试商品置为 ARCHIVED:{product_id}")
        else:
            self.note(f"清理失败,请手动处理商品 {product_id}")
        if template_id:
            # 停用而不是留着:停用的模板不会出现在创建任务的下拉里
            response = self.client.post(f"/api/model-templates/{template_id}/disable")
            if response.status_code == 200:
                self.note(f"已停用冒烟模特模板:{template_id}")
            else:
                self.note(f"停用失败,请手动处理模特模板 {template_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端冒烟测试")
    parser.add_argument("--base-url", default=BASE_URL_DEFAULT)
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--timeout", type=float, default=180.0, help="等任务跑完的上限")
    parser.add_argument("--keep", action="store_true", help="保留测试数据,不归档")
    args = parser.parse_args()

    tag = uuid.uuid4().hex[:6].upper()
    smoke = Smoke(args.base_url, timeout=30.0)

    print(f"\n端到端冒烟测试 · {args.base_url} · Provider={args.provider}\n")

    print("1. 服务健康")
    if not smoke.health():
        print(f"\n{RED}后端没就绪,后面的步骤没有意义{RESET}\n")
        return 1

    print("\n2. 商品与素材")
    product_id = smoke.create_product(tag)
    if not product_id or not smoke.upload_assets(product_id):
        return 1
    # 已授权模特是生成任务的前置:没有它,任务要么被拒,要么走 STATUS.md
    # 里那条跳过 §10.5/§11 的绕行缝 —— 冒烟不该走缝
    template_id = smoke.create_model_template(tag)
    if not template_id:
        return 1

    print("\n3. 生成任务")
    task_id = smoke.create_task(product_id, args.provider, template_id)
    if not task_id:
        return 1
    detail = smoke.wait_for_task(task_id, args.timeout)
    if detail is None:
        return 1

    print("\n4. 流水线结果")
    pipeline_ok = smoke.check_pipeline(detail)

    print("\n5. 评分与分档")
    smoke.check_evaluations(task_id)

    if pipeline_ok:
        print("\n6. 网站图片输出")
        smoke.check_outputs(product_id)

        print("\n7. 导出")
        smoke.check_export_csv(product_id)

    print("\n8. 仪表盘与 Provider")
    smoke.check_dashboard()
    smoke.check_providers()

    if not args.keep:
        print("\n9. 清理")
        smoke.cleanup(product_id, template_id)
    else:
        print(f"\n{YELLOW}已保留测试数据:商品 {product_id},模特模板 {template_id}{RESET}")

    print("\n" + "=" * 60)
    if smoke.failures:
        print(f"{RED}{len(smoke.failures)} 项失败{RESET}:")
        for item in smoke.failures:
            print(f"  - {item}")
        print()
        return 1
    print(f"{GREEN}全部 {smoke.step} 项通过 —— 闭环可用{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
