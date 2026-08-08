#!/usr/bin/env python3
"""A45-batch18 变异验证:方案面板的宿主页与它依赖的那把主键。

用法:python3 tools/mutate_batch18.py

## 这一批在防什么

本批还的是接线欠账,而接线欠账的失效方式**几乎全部是绿的**:

    宿主页没挂路由        页面在仓库里存在,tsc 给一个没人引用的组件开绿灯
    链接用了字符串码      每一行都长出链接,看起来比之前更完整,点进去 422
    取数侧不传 spu_id     出参那一列恒为 null,链接全部消失,接口 200
    主键冲突时挑一个      运营点进去看到的是另一个款,每一步都不报错

四种都不报错,所以四种都得有一条会红的守卫。这份脚本验的就是那四条
**真的咬得住** —— 一条只在今天绿着的守卫,和没有守卫是一回事。

## 为什么变异要动前端文件

本批的判定一半在纯层(主键收敛)、一半在跨语言守卫(宿主页可达性)。
后者读的是 `frontend/src/**` 的源码,所以变异必须能改到那些文件。
沙箱复制的是整个项目树,不是只有 `backend/`(batch14 那次踩过:
沙箱只复制 backend 时,一条要读项目根文件的守卫在**每一次**变异里都红,
把三条真绿伪装成了红)。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

#: 路径基准是 `backend/`,与 `tools/audit_anchors.py` 的 `MUTATIONS` 形状一致。
#: 前端文件因此写成 `../frontend/...` —— 基准是全仓一条的约定,不是这份脚本
#: 自己的选择:审计按固定基准找文件,写成项目根相对会让 14 条锚点全部"找不到"
#: 而变异脚本本身照跑不误(它自己解析得到),那正是"锚点失准不报错"的形状。
SPU = "app/workbench/spu.py"
API = "app/api/workbench_batch.py"
APP = "../frontend/src/App.tsx"
HOST = "../frontend/src/pages/SpuDetailPage.tsx"
AGGREGATE = "../frontend/src/pages/WorkbenchSpuPage.tsx"
CREATE = "../frontend/src/pages/SpuCreatePage.tsx"

#: (编号, 一句话, 相对项目根的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- A 组:主键的收敛
    (
        "A1",
        "冲突时挑一个出来(运营点进去看到的是另一个款,而每一步都不报错)",
        SPU,
        "        if len(ids) != 1:\n            return None",
        "        if not ids:\n            return None",
    ),
    (
        "A2",
        "空值也算一种取值(没迁完的行会把整组判成冲突,链接凭空消失)",
        SPU,
        "        ids = {row.spu_id for row in self.skus if row.spu_id}",
        "        ids = {row.spu_id for row in self.skus}",
        # 半迁移那条守卫会红。它钉的正是「混合期给主键」这个决定
    ),
    (
        "A3",
        "属性恒给 None(链接全站消失,而聚合页看起来完全正常)",
        SPU,
        "        ids = {row.spu_id for row in self.skus if row.spu_id}",
        "        ids = set()",
    ),
    (
        "A4",
        "出参不再带主键(前端只剩字符串码,退回本批之前)",
        SPU,
        '            "spu_id": g.spu_id,\n',
        "",
    ),
    (
        "A5",
        "`SkuRow` 上那一列被删掉(取数侧传了也没地方放)",
        SPU,
        "    spu_id: str | None = None",
        "    legacy_spu_id: str | None = None",
    ),
    # -------------------------------------------------- B 组:取数侧
    (
        "B1",
        "构造点不写 spu_id(列在、判定在,**没有人写它** —— §3.38 那一类)",
        API,
        "                    spu_id=str(product.spu_id) if product.spu_id else None,\n",
        "",
    ),
    (
        "B2",
        "拿字符串码顶主键(前端会把它塞进收 UUID 的接口,得到 422)",
        API,
        "spu_id=str(product.spu_id) if product.spu_id else None,",
        "spu_id=str(product.spu) if product.spu else None,",
    ),
    # -------------------------------------------------- C 组:宿主页可达性
    (
        "C1",
        "宿主页没挂路由(页面在仓库里存在,在界面上不存在)",
        APP,
        '        <Route path="/spus/:spuId" element={<SpuDetailPage />} />\n',
        "",
    ),
    (
        "C2",
        "宿主页不再 import 面板(这一批之前的原样状态)",
        HOST,
        "import GenerationPlanPanel from '../components/GenerationPlanPanel'",
        "// import GenerationPlanPanel from '../components/GenerationPlanPanel'",
    ),
    (
        "C3",
        "聚合页的链接改用字符串码(每一行都长出链接,点进去 422)",
        AGGREGATE,
        "to={`/spus/${row.spu_id}`}",
        "to={`/spus/${row.spu}`}",
    ),
    (
        "C4",
        "建档完成页不再通向方案配置(刚建的款失去唯一那条入口)",
        CREATE,
        "onClick={() => navigate(`/spus/${created.id}`)}",
        "onClick={() => navigate('/workbench-spus')}",
    ),
    (
        "C5",
        "宿主页不再按主键取数(它手上的 spuId 成了未经证实的路由参数)",
        HOST,
        "queryFn: () => spusApi.get(spuId),",
        "queryFn: async () => null as never,",
    ),
    # -------------------------------------------------- D 组:第二份判定
    (
        "D1",
        "宿主页自己挑生效方案(硬规则 4:同一条规则有了第二个答案)",
        HOST,
        "            <GenerationPlanPanel spuId={spu.id} variantLabels={variantLabels} />",
        "            {spu.status === 'ACTIVE' && (\n"
        "              <GenerationPlanPanel spuId={spu.id} variantLabels={variantLabels} />\n"
        "            )}",
    ),
    (
        "D2",
        "宿主页手写一份颜色显示名回落(同一个颜色在两个页面叫两个名字)",
        HOST,
        "    (spu?.color_variants ?? []).map((v) => [v.id, colorVariantLabel(v)]),",
        "    (spu?.color_variants ?? []).map((v) => "
        "[v.id, v.working_name || v.variant_code]),",
    ),
]

SUITE_FILTER = "test_a45_batch18_plan_host.py"


@contextmanager
def _temporary_directory():
    """建一个 Windows 受限令牌也能继续写入的临时目录(同 batch17-2)。"""
    path = Path(tempfile.gettempdir()) / f"batch18-{uuid4().hex}"
    path.mkdir(parents=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _ignore_copy(_directory: str, names: list[str]) -> set[str]:
    exact = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".import_linter_cache",
        "node_modules",
        "__pycache__",
        "dist",
    }
    return {
        name
        for name in names
        if name in exact or name.startswith((".tmp", ".mutation"))
    }


def run_suite(cwd: Path, name_filter: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1800,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    red = 0
    for ident, description, rel, old, new in MUTATIONS:
        with _temporary_directory() as tmp:
            root = tmp / "project"
            shutil.copytree(PROJECT, root, symlinks=True, ignore=_ignore_copy)
            target = (root / "backend" / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")
            code, _ = run_suite(root / "backend", SUITE_FILTER)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
