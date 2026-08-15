#!/usr/bin/env python3
"""a51 变异验证:建档试算 + SPU 停用/恢复。

用法:python3 tools/mutate_a51.py

## 为什么用 copy/restore 而不是反向替换

a48 的交接里记着这一次翻车,值得原样带过来:`sed 's/...$/'` 在 CRLF 文件上
锚点根本不匹配,而 sed 不报错;于是"变异"没生效、测试照旧全绿,
差一点被读成"守卫不设防"。

所以这里的每一条变异都先断言**锚点命中恰好一次**,再改,再跑,再还原。
一个没命中的锚点和一个没有锚点是一回事。

## 行尾:按整个文件猜一种是不够的

第一版这里写的是「文件里有 CRLF 就把锚点全转成 CRLF」。**这棵树里好几个文件
是混合的**(老代码 CRLF、后来补的几段 LF),`app/services/spu_service.py` 就是,
于是 S3 那一条的锚点落在一段 LF 区域上,命中 0 次 —— 幸好断言在,
否则它会被读成"守卫没红"。现在两种形式都试,合起来必须恰好命中一次。

## 第一遍跑下来三条没红,其中两条是守卫在说假话

    R1  值相等证明不了出处   `"max_variant_code": MAX_VARIANT_CODE` 换成 `16`,
                             守卫比的是值,而两者的值恰好相等
    S1  正向断言吃到了注释   整句调用换掉之后,断言命中的是同一个函数
                             docstring 里的那串字。`audit_source_guards.py`
                             只管反向断言,正向这个方向全仓没人管

两条都已修好(前者改走 AST,后者改吃去注释的源码),结论记在
`docs/DECISIONS.md` §3.77 第三节。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SUITE = "tests/pure/test_a51_spu_preview_and_lifecycle.py"

MATRIX = "app/listings/sku_matrix.py"
SERVICE = "app/services/spu_service.py"
GATE = "app/api/action_gate.py"
CREATE_PAGE = "../frontend/src/pages/SpuCreatePage.tsx"
DETAIL_PAGE = "../frontend/src/pages/SpuDetailPage.tsx"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
#:
#: 描述写的是**这个改动在生产上的表现**,不是"哪条断言会不满足"。
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 规则下发:第二个版本
    (
        "R1",
        "规则端点把常量抄成字面量 —— 第二个版本就是这样长出来的",
        MATRIX,
        '"max_variant_code": MAX_VARIANT_CODE,',
        '"max_variant_code": 16,',
    ),
    (
        "R2",
        "字符集不排序:同一份规则每次刷新换一个顺序,运营会以为规则变了",
        MATRIX,
        '"".join(sorted(_ALLOWED))',
        '"".join(_ALLOWED)',
    ),
    (
        "R3",
        "颜色三条判定被抄回 expand,试算从此是第二个版本",
        MATRIX,
        "    normalized = list(normalize_variant_codes(variant_codes))",
        "    if not variant_codes:\n"
        '        raise SkuPlanError("variant_codes", "至少要有一个颜色变体")\n'
        "    normalized = [str(c).strip().upper() for c in variant_codes]",
    ),
    # -------------------------------------------------- 试算:形状对、答案假
    (
        "P1",
        "试算不再真查编码占用,回一个好看的常量(硬规则 4 点名的形状)",
        SERVICE,
        '"code_taken": _code_taken(session, spu_code),',
        '"code_taken": False,',
    ),
    (
        "P2",
        '"算不出"退化成 0 —— 界面上它会被读成"这次建档一行都不产生"',
        SERVICE,
        '"sku_count": len(planned) if template is not None else None,',
        '"sku_count": len(planned),',
    ),
    # -------------------------------------------------- 停用:闸与语义
    (
        "S1",
        "停用绕过平台闸:平台上还挂着的款可以在本地被停掉",
        SERVICE,
        "    live = product_service.live_listings_for(session, [row.id for row in skus])",
        "    live = []",
    ),
    (
        "S2",
        "停用连带把底下的 SKU 一起归档(十几条审计记录的理由全是同一句)",
        SERVICE,
        "    previous = spu.status\n    spu.status = SpuStatus.DISABLED.value",
        "    previous = spu.status\n"
        "    for row in skus:\n"
        "        row.status = ProductStatus.ARCHIVED.value\n"
        "    spu.status = SpuStatus.DISABLED.value",
    ),
    (
        "S3",
        "停用中的 SPU 又能长出新颜色 ——「停用」退化成一个标签",
        SERVICE,
        "    _assert_open_for_building(spu)\n    existing = skus_of(session, spu_id)",
        "    existing = skus_of(session, spu_id)",
    ),
    (
        "S4",
        "列表过滤停用、总数不过滤:最后一页是空的,而空态写着「还没有商品」",
        SERVICE,
        "        session.scalar(_visible(select(func.count(Spu.id)), include_disabled)) or 0",
        "        session.scalar(select(func.count(Spu.id))) or 0",
    ),
    (
        "G1",
        "新写端点没进闸表(a50 实测过:红了两个提交没人看见)",
        GATE,
        '    "spus:disable_spu": "停用把整个款移出建档动线,不是往前走一步",\n',
        "",
    ),
    # -------------------------------------------------- 前端:接线断在最后一跳
    (
        "F1",
        "后端点名的 loc 又被丢掉 —— 红字弹出来,却指不到任何一个输入框",
        CREATE_PAGE,
        "  const onWriteError = useWriteError(noRefresh, onFields)",
        "  const onWriteError = useWriteError(noRefresh)",
    ),
    (
        "F2",
        "前端自己乘出 SKU 总数:它和后端算的通常一样,而通常一样最难发现",
        CREATE_PAGE,
        "                  value={preview?.sku_count ?? '—'}",
        "                  value={colours.length * sizes.length}",
    ),
    (
        "F3",
        "乐观锁版本号写死:轻则第二次保存起全部撞 409,重则覆盖别人的改动",
        DETAIL_PAGE,
        "        expected_version: spu!.row_version,",
        "        expected_version: 1,",
    ),
    (
        "F4",
        "按钮改叫「删除」—— 第一个点它的人会以为库里那些行没了",
        DETAIL_PAGE,
        "                  停用这个款\n",
        "                  删除这个款\n",
    ),
]


def run(entry: tuple[str, str, str, str, str]) -> bool:
    name, why, rel, anchor, replacement = entry
    path = (BACKEND / rel).resolve()
    original = path.open(encoding="utf-8", newline="").read()

    # CRLF 文件上直接找 LF 锚点是找不到的 —— 这就是 a48 那次翻车的形状,
    # 而按整个文件猜一种行尾也不够(见模块文档)。两种形式都试,
    # 合起来必须恰好命中一次
    forms = list(dict.fromkeys([anchor, anchor.replace("\n", "\r\n")]))
    matched = [(f, original.count(f)) for f in forms if original.count(f)]
    total = sum(n for _, n in matched)
    if total != 1:
        print(f"  {name}  锚点命中 {total} 次(期望 1)—— 变异没做,视同失败")
        return False
    probe = matched[0][0]
    body = replacement.replace("\n", "\r\n") if "\r\n" in probe else replacement

    backup = Path(tempfile.mkdtemp()) / path.name
    shutil.copy2(path, backup)
    try:
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(original.replace(probe, body, 1))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header", "-x"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
        )
        red = result.returncode != 0
        print(f"  {name}  {'变红' if red else '**没红**':6}  {why}")
        return red
    finally:
        shutil.copy2(backup, path)


if __name__ == "__main__":
    wanted = set(sys.argv[1:])
    entries = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    print(f"a51 变异验红({len(entries)} 条)")
    ok = all([run(m) for m in entries])
    print("全部按预期变红" if ok else "有守卫没红 —— 它加了等于没加")
    sys.exit(0 if ok else 1)
