"""A46-phase5:文档审核收口。四类缺陷,一个共同点 —— **两份真相分叉,而没有东西在看**。

本轮修的东西表面上互不相干:一段部署文档、三份 `AGENTS.md`、一句「只有管理员
看得见」、几个引用了不存在文件的注释。但它们坏的方式是同一个:

    说法在 A 处被订正,而 B 处继续说旧的       STATUS 订正了菜单收敛,三个 .tsx 没跟上
    守卫只钉一份文件,读者打开的是另一份       三把启动键只钉 README,部署的人读 DEPLOYMENT
    副本没有同步机制                          AGENTS.md 停在被订正掉的那一版
    注释宣告一个不存在的守卫                  `test_browser_login_frontend.py` 全仓没有

第四类最贵:读到「那边已经钉住了」的人**不会再去那边看**。这正是 §3.33 与
`test_a45_batch16_doc_truth.py` 说的同一件事,本文件是它在 a46 这一轮的续。

## 写法上的两条纪律

**钉一致性,不钉现状(§3.31 / §3.33)。** 下面每一条都能同时容纳两种世界:
菜单那条在「裁剪了」和「没裁剪」下都能绿,只拒绝「代码一个样、注释另一个样」;
文档地图那条不关心地图有几份,只要求那句话和表格行数是同一个数。钉现状的守卫
会因为进步而变红,而那种红会训练人去改守卫。

**引用一个已经不在的文件不算错,说它「正在钉着」才算。** 所以下面判的是
**时态**,不是存在性:同一段里带了「原先 / 已并入 / 当年 / 退役」这类过去式标记
的,一律放行 —— 那是在记录历史,而历史正是这个仓库的注释密度所要保留的东西。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "frontend"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    """按空行/注释骨架切段。**不用切片** —— 反向断言不许吃来路不明的窗口。

    段落是「连续的、有内容的注释行」。`*`、`/**`、`*/`、`{/*`、`*/}` 和空行都算
    分隔符,因为 jsdoc 里那一列星号本身不携带语义。
    """
    skeleton = {"", "*", "/**", "*/", "{/*", "*/}", "#", "//"}
    out: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip().lstrip("*").lstrip("/").strip()
        if stripped in skeleton:
            if current:
                out.append("\n".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        out.append("\n".join(current))
    return out


# ======================================================== 一、AGENTS.md 不许分叉

#: 三对。左边是唯一真相,右边是给读 `AGENTS.md` 的工具的副本。
_DOC_COPIES = (
    (PROJECT_ROOT / "CLAUDE.md", PROJECT_ROOT / "AGENTS.md"),
    (BACKEND_ROOT / "CLAUDE.md", BACKEND_ROOT / "AGENTS.md"),
    (FRONTEND / "CLAUDE.md", FRONTEND / "AGENTS.md"),
)


def test_every_agents_md_is_a_verbatim_copy_of_its_claude_md():
    """`AGENTS.md` 必须与同级 `CLAUDE.md` 逐字一致。

    ## 为什么要有这一条

    a46-phase5 之前,三份 `AGENTS.md` 都是 `CLAUDE.md` 的**旧副本**(正文首行
    还写着 `# CLAUDE.md — 仓库总纲`),而且已经漂了:根那份差 113 行,其中包括
    一整段被订正掉的文案 —— 它写着「一条命令跑全部:`make check`」,而
    `CLAUDE.md` 早已改成「`make check` 跑绿也 ≠ CI 会绿:两者都不跑 pytest,
    覆盖不到 backend / e2e / images 三个 job」。

    也就是说:Claude Code 读到的是订正后的,别的 agent 读到的是**那条被点名
    修过的错**。全仓 0 处引用 `AGENTS.md`、0 道门禁盯着它,分叉不会有任何征兆。

    ## 为什么是「逐字一致」而不是「指针文件」

    指针文件(`见 CLAUDE.md`)更省事,但读 `AGENTS.md` 的工具多半不会跟着跳
    第二跳,拿到的就是一行废话。副本 + 守卫是两害相权:重复一份内容,换掉
    「两份约定同时存在且不同」这个更坏的形状。改约定只改 `CLAUDE.md`,再同步。
    """
    for source, copy in _DOC_COPIES:
        assert copy.exists(), f"{copy} 不见了 —— 读 AGENTS.md 的工具会一无所获"
        assert _read(copy) == _read(source), (
            f"{copy.relative_to(PROJECT_ROOT)} 与 {source.relative_to(PROJECT_ROOT)} "
            "分叉了。两份约定同时存在且不同,比只有一份过期的更坏 —— "
            "读哪一份取决于用的是哪个工具。改约定改 CLAUDE.md,再把它整份拷过去。"
        )


# ==================================================== 二、菜单收敛的说法与代码同向

#: 这几句话只可能是在断言「菜单按账号裁剪」。
_ROLE_TRIM_CLAIMS = (
    "只有管理员看得见",
    "对非管理员隐藏",
    "菜单收敛之后",
    "让菜单长出",
    "只对管理员显示",
)

#: 带上其中任意一个,这段话就是在讲历史或讲「没有落地」,不是在断言现状。
_PAST_MARKERS = (
    "原先",
    "原文",
    "已过期",
    "没有落地",
    "那一版没落地",
    "不按账号隐藏",
    "不按角色",
    "已并入",
    "并进",
    "退役",
    "当年",
    "曾经",
    "早已",
    "订正",
    # 「这个文件不存在」本身就是最直白的过去式:说出来的人没有在宣告一个守卫。
    # 本文件自己的 docstring 需要它 —— 它要引用那个幽灵名字才能说清事情。
    "不存在",
)

_MENU_SOURCES = (
    FRONTEND / "src" / "App.tsx",
    FRONTEND / "src" / "components" / "AppLayout.tsx",
    FRONTEND / "src" / "hooks" / "useIdentity.ts",
    BACKEND_ROOT / "app" / "api" / "auth.py",
)


def _menu_is_trimmed_by_role() -> bool:
    """菜单**实际上**按账号裁剪了没。两处证据都要成立。

    判据是 `and` 不是 `or`,而这一版是被一条变异逼出来的:a46-phase6 把过滤器
    拆掉、只留 `NAV` 上的 `adminOnly` 声明时,`or` 版本仍然回答"裁剪了"——
    于是三个文件里"只有管理员看得见"的说法全部放行,而实际上**每个人都看得见**。
    一个声明了却没有人读的标记,和没有这个标记是一回事;两处缺一,
    菜单就不是按角色裁剪的。
    """
    nav = _read(FRONTEND / "src" / "App.tsx")
    layout = _read(FRONTEND / "src" / "components" / "AppLayout.tsx")
    return "adminOnly" in nav and "groups.filter" in layout


def test_the_menu_role_claims_agree_with_what_the_menu_actually_does():
    """「菜单按角色隐藏」这句话,和 `NAV` 的实际形状必须同向。

    ## 这条守的是一次真实的漏改

    A8 计划里写的是「侧栏按角色收敛」。落地时**翻转了**:`NAV` 里没有
    `adminOnly`,`AppLayout` 不按 `is_admin` 过滤菜单,
    `nav-and-url-filters.test.tsx` 反过来钉着「不按账号隐藏」,
    `docs/STATUS.md` 也已按 a46-phase2 订正过。

    而三个 `.tsx` / `.ts` 文件里的说明**一个都没跟上**,其中 `App.tsx` 自己
    的文件头说「按角色收敛」「只有管理员看得见」,同一个文件往下 60 行的路由
    注释说「路由和菜单都不按角色裁剪」—— 一个文件里两句话互为反面。
    `LOCAL_MANUAL_TEST.md` §4.5 第 5 步还专门让验收的人「理由写在 App.tsx 里」
    去读它。

    ## 判据

    同时容纳两种世界:真的裁剪了,这些话就该在;没裁剪,就必须带过去式标记。
    只拒绝「代码一个样、注释另一个样」。
    """
    trimmed = _menu_is_trimmed_by_role()
    for path in _MENU_SOURCES:
        for para in _paragraphs(_read(path)):
            claims = [c for c in _ROLE_TRIM_CLAIMS if c in para]
            if not claims:
                continue
            marked = any(m in para for m in _PAST_MARKERS)
            if trimmed:
                assert not marked, (
                    f"{path.name} 这一段把菜单裁剪说成过去式,而 NAV/AppLayout "
                    f"里现在**真的**在按账号裁剪:\n{para}"
                )
            else:
                assert marked, (
                    f"{path.name} 这一段断言菜单按账号隐藏({claims}),而 NAV 里"
                    f"没有 adminOnly、AppLayout 也不按 is_admin 过滤菜单 —— "
                    f"说法和代码反了:\n{para}\n"
                    "要么把这段改成过去式(说明那一版没有落地),要么真的去裁剪菜单。"
                )


# ============================================ 三、注释里宣告的守卫,必须真的存在

#: 只扫活代码。`docs/` 是历史台账,那里提一个已删掉的文件名是正常的。
_LIVE_CODE_DIRS = (
    BACKEND_ROOT / "app",
    BACKEND_ROOT / "tools",
    BACKEND_ROOT / "tests" / "pure",
    FRONTEND / "src",
    PROJECT_ROOT / "tools",
)

_GUARD_NAME = re.compile(r"`([A-Za-z0-9_.\-]+\.(?:py|tsx|ts))`")


def _live_code_files() -> list[Path]:
    out: list[Path] = []
    for root in _LIVE_CODE_DIRS:
        for path in sorted(root.rglob("*")):
            if path.suffix in {".py", ".ts", ".tsx", ".sh", ".ps1"}:
                if "__pycache__" not in str(path):
                    out.append(path)
    return out


def _every_filename_in_the_tree() -> set[str]:
    return {p.name for p in PROJECT_ROOT.rglob("*") if p.is_file()}


def test_no_live_comment_claims_a_guard_file_that_does_not_exist():
    """活代码里以**现在时**引用的守卫文件,必须真的在树上。

    ## 这一条比「路径指得到」严格,也比它窄

    `tools/audit_doc_refs.py` 已经在管路径引用,但它自己在输出里写着:
    「这只管路径存不存在 —— 一句指得到但说错内容的话,它看不出来」。而且它只
    认带斜杠的路径,反引号里一个**裸文件名**它看不见。

    a46 撞见的正是裸文件名:`backend/app/api/health.py` 与
    `frontend/src/api/client.ts` 都写着「`test_browser_login_frontend.py`
    钉着这一点」「所以它有一条源码级守卫(...)」—— 那个文件全仓不存在。
    真正在钉的是 `test_a46_phase2_browser_login_seam.py`。

    这类句子的代价不是「多走弯路」,是**不走** —— 读到「那边已经覆盖了」的人
    不会再去那边看有没有东西。

    ## 时态,不是存在性

    引用一个已经不在的文件本身没问题,这个仓库到处在记「原先在哪儿、后来并进
    了谁」。所以只要同一段里带了过去式标记就放行。判的是那句话有没有在**宣告
    一件没发生的事**。
    """
    existing = _every_filename_in_the_tree()
    problems: list[str] = []
    for path in _live_code_files():
        for para in _paragraphs(_read(path)):
            for name in _GUARD_NAME.findall(para):
                if not name.startswith("test_") and ".test." not in name:
                    continue
                if name in existing:
                    continue
                if any(m in para for m in _PAST_MARKERS):
                    continue
                problems.append(
                    f"{path.relative_to(PROJECT_ROOT)} 说 `{name}` 在钉着什么,"
                    f"而这个文件不存在:\n{para}"
                )
    assert not problems, (
        "下面这些注释宣告了一个不存在的守卫。读到它的人不会再去找真的那一条:\n\n"
        + "\n\n".join(problems)
    )


# ================================================== 四、文档地图的数和表格是同一个

_STATUS = PROJECT_ROOT / "docs" / "STATUS.md"


def _doc_map_section() -> str:
    text = _read(_STATUS)
    start = text.index("## 七、文档地图")
    end = text.index("另有 `docs/vendor/", start)
    return text[start:end]


def test_the_doc_map_says_the_same_number_as_its_own_table():
    """「一共 N 份」必须等于表格行数,「眼下 M 份」必须等于真的还剩几份过程文档。

    地图这一节自己立的规矩是「每份都写明什么时候看 —— 回答不了就不该留下」,
    而 a46-phase5 之前它写着「一共 13 份」、表里 15 行,并且漏收了四份活文档
    (`LOCAL_MANUAL_TEST.md` / `docs/MANUAL-ACCEPTANCE.md` / `HANDOVER.md` /
    `AGENTS.md`)。「眼下 29 份」那个数则停在过程文档被大批收编之前。

    不钉「地图该有几份」—— 增删文档是正常的。只钉那句话和表格是同一个数。
    """
    section = _doc_map_section()
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    declared = re.search(r"一共 (\d+) 份", section)
    assert declared, "文档地图开头那句「一共 N 份」不见了 —— 这条守卫失去了对象"
    assert int(declared.group(1)) == len(rows), (
        f"文档地图说「一共 {declared.group(1)} 份」,而表里有 {len(rows)} 行。"
        "这个数过期不会有任何东西报错,它只是让读者以为自己看全了。"
    )

    docs_dir = PROJECT_ROOT / "docs"
    on_map = {name for name in re.findall(r"`([^`]+\.md)`", section)}
    process = [
        p
        for p in sorted(docs_dir.glob("*.md"))
        if p.name not in on_map
        and (
            p.name.startswith("MERGE-")
            or p.name.startswith("REVIEW-A4")
            or p.name.startswith("REVIEW-STAGE")
        )
    ]
    left = re.search(r"眼下 (\d+) 份", section)
    assert left, "「眼下 M 份」那句不见了"
    assert int(left.group(1)) == len(process), (
        f"地图说地图外还有 {left.group(1)} 份过程文档,实际是 {len(process)} 份:"
        f"{[p.name for p in process]}"
    )


# ============================================ 五、示例数据的条数:要么不写,要么是真的

_SAMPLE_IMAGES = PROJECT_ROOT / "sample-data" / "images"

#: 这几份是给「第一次接触」和「要部署」的人看的,条数一律不写死。
_NO_SAMPLE_COUNT_DOCS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CLAUDE.md",
    PROJECT_ROOT / "AGENTS.md",
    PROJECT_ROOT / "docs" / "DEPLOYMENT.md",
    PROJECT_ROOT / "docs" / "MANUAL-ACCEPTANCE.md",
)

_SAMPLE_TOTAL_CLAIM = re.compile(r"(\d+)\s*张\s*(?:示例|占位|素材)|(\d+)\s*个示例商品")


def test_the_general_docs_do_not_freeze_a_sample_data_count():
    """入口文档不许写死示例数据的条数。

    「10 个商品 + 30 张素材」这句话在五份文档里各冻了一份,而实际早就是 51 张。
    `LOCAL_MANUAL_TEST.md` 里甚至已经写着「这份文件的『10 个 SKU + 30 张图』
    就过期过一次」并改成让人跑 `verify_sample_data.py` —— **教训只在踩到它的
    那一份文件里生效了**,隔壁四份原样冻着。

    所以这条不是「把 30 改成 51」(下次增删样例又会过期),是「这几份文档里
    不出现这个数」。要当前口径的人跑 `backend/tools/verify_sample_data.py`。

    `LOCAL_MANUAL_TEST.md` 与 `sample-data/README.md` 不在名单里:前者是那条
    教训的出处,里面的数字是引用旧文案;后者就贴着数据本身,由下一条钉真值。
    """
    for doc in _NO_SAMPLE_COUNT_DOCS:
        hit = _SAMPLE_TOTAL_CLAIM.search(_read(doc))
        assert hit is None, (
            f"{doc.relative_to(PROJECT_ROOT)} 又把示例数据的条数写死了:"
            f"「{hit.group(0)}」。增删样例时它会静默过期,而没有任何东西会报错 —— "
            "改成指向 backend/tools/verify_sample_data.py。"
        )


def test_the_sample_data_readme_states_the_real_image_count():
    """`sample-data/README.md` 可以写数,但那个数必须是真的。

    它贴着数据本身,读者是「想知道这批样例怎么组织的」那个人,一个具体的数
    对他有用。代价是它会过期 —— 所以由这条钉着它和文件树一致。
    """
    readme = PROJECT_ROOT / "sample-data" / "README.md"
    actual = len([p for p in _SAMPLE_IMAGES.iterdir() if p.is_file()])
    declared = re.search(r"(\d+)\s*张\s*\d+×\d+", _read(readme))
    assert declared, "sample-data/README.md 里那句「N 张 900×1200 JPEG」不见了"
    assert int(declared.group(1)) == actual, (
        f"sample-data/README.md 说 {declared.group(1)} 张,实际 {actual} 张。"
    )


# ============================================ 六、硬错误代码的条数不许冻在 README 里

def _hard_fail_code_members() -> int:
    tree = ast.parse(_read(BACKEND_ROOT / "app" / "core" / "enums.py"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "HardFailCode":
            return len(
                [
                    stmt
                    for stmt in node.body
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign))
                ]
            )
    raise AssertionError("enums.py 里找不到 HardFailCode —— 这条守卫失去了对象")


def test_the_readme_does_not_freeze_the_hard_fail_code_count():
    """README 说到硬错误代码时,要么不写数,要么写对。

    它原来写着「15 个硬错误代码」,而 `HardFailCode` 现在是 21 条(受众拆分之后
    女装/男装各一组,又加了三条通用的)。讽刺的是同一份 README 的测试一节刚
    写完「根因不是数字抄错,是把一个每批都在变的事实复制进了散文」——
    隔两屏自己又犯了一次。

    这条同时接受两种写法,只拒绝**写了一个错的数**。
    """
    readme = _read(PROJECT_ROOT / "README.md")
    hit = re.search(r"(\d+)\s*个硬错误代码", readme)
    if hit is None:
        return
    assert int(hit.group(1)) == _hard_fail_code_members(), (
        f"README 说「{hit.group(0)}」,而 HardFailCode 有 "
        f"{_hard_fail_code_members()} 条。"
    )
