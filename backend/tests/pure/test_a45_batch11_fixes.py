"""A45 batch11:对独立审查(Codex)C-01~C-11 / L-01 的逐条整改守卫。

裁决与修复的完整对照见交付说明;这里只守**已修的那些**:

    C-02 / L-01   前端构建阻断(未用导入、mutation 变量类型)
    C-03          受众 × 品类:显式清空语义 + 后端组合校验 + SPU 传播
    C-04          受众变更 → 已批准图片集降级待复核
    C-05          受众变更 → 旧草稿立即 STALE;提交路径补齐指纹/受众双闸
    C-06          审计 payload JSON 安全化(datetime 进 JSONB 曾整笔回滚)
    C-07 / C-08   模板 PATCH:合并后授权时间窗;非空列拒绝显式 null
    C-09          授权编辑只发脏字段
    C-01 / C-11   交付必备文件复验;冒烟改走已授权模板并如实声明覆盖边界

与 batch10 同一取向:凡"后端护栏在、前端绕开"或"两份词表各写一份"的形状,
守卫必须跨语言 —— 光断言后端不会变红。

零三方依赖:只 import `core/json_safe`、`core/garments`、`core/enums`
与若干 TS/PY 源文本。可用裸 python3 经 `tools/run_pure_tests.py` 执行。
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import types
import uuid

from app.core import garments
from app.core.enums import Audience, GarmentType
from app.core.json_safe import json_safe
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _be(rel: str) -> str:
    return _read(BACKEND_ROOT / "app" / rel)


def _fe(rel: str) -> str:
    return _read(FRONTEND / rel)


def _code_only(source: str) -> str:
    """去掉 TS/TSX 注释再做字符串断言(batch10 的教训:注释里出现一次就能骗过)。"""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


def _py_code_only(source: str) -> str:
    """同上,但针对 Python:只去掉**整行**注释。

    这个仓库删一段代码时习惯把原文以注释形式留在原处(记着它为什么被删),
    于是"这一行不许再出现"的断言会被那段注释骗成红的 —— 本文件里
    `test_the_model_reference_bypass_is_closed` 第一版正是这么假红的。

    刻意只处理整行注释:行尾 `#` 可能落在字符串字面量里,而把
    `foo("a # b")` 截成 `foo("a ` 会造出比它要防的问题更难看的假红。
    """
    return re.sub(r"(?m)^\s*#.*$", "", source)


# ================================================================ C-06 审计 JSON 安全


def test_the_exact_batch10_audit_payload_crashes_without_json_safe():
    """先钉住病灶本身:datetime 直塞 `json.dumps` 就是当时的 TypeError。

    JSONB 列的默认序列化器就是 `json.dumps`(`db/session.py` 没配
    `json_serializer`),`update_template` 把 `_naive_utc` 归一出来的
    datetime 原样放进了 `_AUDITED_FIELDS` 点名的审计 payload —— 于是
    修改授权到期日的 PATCH 在 commit 时整笔回滚。
    """
    payload = {"changed": ["license_expires_at"], "license_expires_at": dt.datetime(2027, 1, 1)}
    try:
        json.dumps(payload)
    except TypeError:
        pass
    else:
        raise AssertionError(
            "datetime 居然能直接 json.dumps 了?那 C-06 的前提变了,重新核对序列化器"
        )
    # 修复后的形状:同一份 payload 过 json_safe 之后必须能序列化,且日期是 ISO
    safe = json_safe(payload)
    blob = json.dumps(safe, ensure_ascii=False)
    assert "2027-01-01T00:00:00" in blob
    assert safe["license_expires_at"] == "2027-01-01T00:00:00"


def test_json_safe_covers_the_types_services_actually_put_in_payloads():
    value = {
        "when": dt.date(2026, 8, 4),
        "who": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "amount": decimal.Decimal("19.90"),
        "grade": GarmentType.BIKINI_SET,          # 枚举 -> value
        "tags": {"b", "a"},                        # set -> 排序 list(审计要可重放比对)
        "nested": [{"at": dt.datetime(2026, 1, 2, 3, 4, 5)}],
        "raw": b"ok",
    }
    safe = json_safe(value)
    json.dumps(safe)  # 不抛即通过第一半
    assert safe["when"] == "2026-08-04"
    assert safe["who"] == "00000000-0000-0000-0000-000000000001"
    assert safe["amount"] == "19.90"               # Decimal 不转 float:金额不丢精度
    assert safe["grade"] == "BIKINI_SET"
    assert safe["tags"] == ["a", "b"]
    assert safe["nested"][0]["at"] == "2026-01-02T03:04:05"
    assert safe["raw"] == "ok"


def test_the_audit_choke_point_applies_json_safe():
    """修在 `audit.record` 一处,而不是那一个调用点 —— 下一个把日期塞进
    payload 的服务不该重演同一次事故。"""
    src = _be("services/audit.py")
    assert "json_safe(redact(" in src, (
        "audit.record 没有对 payload 做 JSON 安全化 —— C-06 的修复被移走了"
    )


# ================================================================ C-03 受众 × 品类


def test_backend_and_frontend_agree_on_garment_types_per_audience():
    """跨语言契约:两份词表逐字、含顺序一致。改口径必须两份一起改。"""
    ts = _read(FRONTEND / "api" / "types.ts")
    block = re.search(
        r"GARMENT_TYPES_BY_AUDIENCE[^=]*=\s*\{(.*?)\n\}", ts, re.S
    )
    assert block, "前端找不到 GARMENT_TYPES_BY_AUDIENCE —— 契约的另一半没了"
    frontend: dict[str, list[str]] = {}
    for name, items in re.findall(r"(\w+):\s*\[(.*?)\]", block.group(1), re.S):
        frontend[name] = re.findall(r"'([A-Z_]+)'", items)
    backend = {
        audience.value: list(values)
        for audience, values in garments.GARMENT_TYPES_BY_AUDIENCE.items()
    }
    assert frontend == backend, (
        "受众×品类词表前后端分叉了:\n"
        f"  前端 {frontend}\n  后端 {backend}\n"
        "改口径要两份一起改 —— 只改一份正是 C-03 发生的方式"
    )
    # 词表并集必须恰好铺满枚举:少一个值,那个品类在所有受众下都选不出来
    union = {v for values in backend.values() for v in values}
    assert union == {v.value for v in GarmentType}


def test_the_garment_gate_logic_itself():
    assert garments.garment_allowed("WOMEN", "BIKINI_SET")
    assert not garments.garment_allowed("MEN", "BIKINI_SET")
    assert garments.garment_allowed("MEN", "SWIM_SHORTS")
    assert not garments.garment_allowed("UNISEX", "SWIM_SHORTS")  # 前端口径:UNISEX 无沙滩裤
    # OTHER 是清空落点,对三个受众恒许
    for audience in Audience:
        assert garments.garment_allowed(audience, "OTHER")
    # 受众未确认不拦("该出哪一组"还没有答案);空品类不拦
    assert garments.garment_allowed(None, "BIKINI_SET")
    assert garments.garment_allowed("MEN", "")
    assert garments.garment_allowed("MEN", None)
    # 未知取值在受众已知时判否 —— 打错字不该以"匹配不上所以放行"收场
    assert not garments.garment_allowed("MEN", "BOARD_SHORTS")
    assert garments.garment_block_reason("MEN", "BIKINI_SET") is not None
    assert garments.garment_block_reason("MEN", "SWIM_SHORTS") is None


def test_update_product_enforces_the_combo_and_resets_explicitly():
    """服务层的接线:显式非法组合拒绝;只改受众时旧品类**显式**写回 OTHER
    (进 applied、进审计),兄弟行同一事务里过同一道校验。"""
    src = _be("services/product_service.py")
    assert "garments.garment_block_reason(product.audience, product.garment_type)" in src
    assert '"garment_type" in changes' in src, "分不出显式与缺省,拒绝/重置就选不对"
    assert "product.garment_type = GarmentType.OTHER.value" in src
    assert "sibling.garment_type = GarmentType.OTHER.value" in src, (
        "只搬受众不看品类,传播自己会制造 MEN + BIKINI_SET"
    )
    assert "garment_type_reset_on" in src, "重置了哪些 SKU 必须进审计"
    # create 不重复这道闸的话,删了重建就能绕开
    create = re.search(r"def create_product\(.*?\n    return product\n", src, re.S)
    assert create and "garment_block_reason" in create.group(0), (
        "create 路径没有组合校验 —— 重建即可绕开 update 的那道"
    )


def test_both_audience_entries_send_an_explicit_garment_type():
    """前端那半:清空必须以显式值上线。undefined 的键会被 JSON.stringify
    整个丢掉,后端按「未传不改」处理 —— batch10 给 audience 修过同一个坑,
    漏了 garment_type 这半。"""
    card = _code_only(_fe("components/workbench/AudienceConfirmCard.tsx"))
    assert "garment_type: pickedType ?? 'OTHER'" in card
    modal = _code_only(_fe("components/ProductFormModal.tsx"))
    assert "garment_type: values.garment_type ?? 'OTHER'" in modal


# ================================================================ C-04 / C-05 级联


def test_audience_change_downgrades_approved_image_sets_in_the_same_transaction():
    service = _be("listings/image_set_service.py")
    fn = re.search(
        r"def downgrade_sets_on_audience_change\(.*?\n    return downgraded\n",
        service, re.S,
    )
    assert fn, "找不到受众变更的图片集降级函数(C-04)"
    body = fn.group(0)
    assert "ImageSetStatus.APPROVED.value" in body
    assert "ImageSetStatus.PENDING_REVIEW.value" in body
    assert "downgrade_on_audience_change" in body, "系统自动降级必须进审计(A-39)"
    # 接线:update_product 在受众变更分支里真的调用它
    product_service = _be("services/product_service.py")
    assert "image_set_service.downgrade_sets_on_audience_change(" in product_service


def test_audience_change_marks_same_spu_drafts_stale_immediately():
    src = _be("services/product_service.py")
    update = re.search(r"def update_product\(.*?\n    return product\n", src, re.S)
    assert update, "找不到 update_product"
    body = update.group(0)
    assert "DraftStatus.STALE.value" in body, "受众变更没有把旧草稿标 STALE(C-05)"
    assert "DraftStatus.ARCHIVED.value" in body, (
        "ARCHIVED 必须豁免 —— 它已归档,上游怎么变都与它无关"
    )
    assert "drafts_marked_stale" in body, "标了多少份必须进审计"


def test_the_submit_path_has_the_same_gates_as_export():
    """提交与导出必须是同一句话。此前 `export_gate` 四道闸,提交只有
    `_assert_submittable` 一道只看存储状态的 —— 改完受众直接点提交,
    旧受众内容照样排队上平台。"""
    api = _read(BACKEND_ROOT / "app" / "api" / "publish.py")
    section = re.search(
        r"def submit\(.*?result = publish_service\.enqueue\(", api, re.S
    )
    assert section, "找不到 submit 到 enqueue 之间的段落"
    body = section.group(0)
    assert "refresh_draft" in body, "提交前没有按当前上游重算指纹(C-05)"
    assert "draft_audience_gate" in body, "提交路径缺受众闸,与导出不对等"
    assert "DRAFT_STALE" in body, "过期草稿必须以 DRAFT_STALE/409 拒绝"
    assert '"DELIST"' in body, (
        "DELIST 豁免消失了 —— 清理预案要下架的恰恰是草稿早已过期的商品"
    )


# ================================================================ C-07 / C-08 模板 PATCH


def test_template_patch_rejects_explicit_null_on_non_nullable_columns():
    src = _be("services/model_template_service.py")
    assert "def _nullable(" in src, "缺 _nullable 守卫(问表,不维护第二张清单)"
    update = re.search(r"def update_template\(.*?\n    return template\n", src, re.S)
    assert update, "找不到 update_template"
    body = update.group(0)
    assert "value is None and not _nullable(key)" in body
    assert "不允许清空" in body, "非空列收到 null 必须换成一句人话,不是数据库 500"


def test_template_patch_validates_the_merged_license_window():
    src = _be("services/model_template_service.py")
    update = re.search(r"def update_template\(.*?\n    return template\n", src, re.S)
    assert update
    body = update.group(0)
    assert "end <= start" in body, (
        "PATCH 侧没有合并后的时间窗校验 —— 创建拒绝的组合,改一改就能存进去(C-07)"
    )
    assert "授权到期时间必须晚于开始时间" in body


# ================================================================ C-02 / L-01 / C-09 前端


def test_the_two_frontend_build_blockers_are_gone():
    card = _fe("components/workbench/AudienceConfirmCard.tsx")
    import_line = next(
        line for line in card.splitlines() if line.startswith("import {") and "antd" in line
    )
    assert "Modal" not in import_line, (
        "未使用的 Modal 导入回来了 —— noUnusedLocals 下 tsc -b 必红(C-02/TS6133)"
    )
    page = _code_only(_fe("pages/ModelTemplatesPage.tsx"))
    assert "Record<string, string | boolean>" in page, (
        "create mutation 的变量类型又窄回 Record<string, string> 了 —— "
        "提交载荷里有三个布尔授权字段,tsc 会报 TS2322(L-01)"
    )


def test_the_license_patch_sends_only_dirty_fields():
    """update mutation 的注释一直承诺「只发改过的键」;batch10 的实现却回发
    整份快照 —— A 改备注会把 B 刚撤销的授权覆盖回旧值。后端没有版本号,
    前端不发的键就是唯一的保护。"""
    page = _code_only(_fe("pages/ModelTemplatesPage.tsx"))
    assert "const unchanged = (" in page, "逐键比对函数没了"
    assert "Object.entries(next).filter" in page, "没有按脏字段过滤,仍是全量快照(C-09)"
    # 日期要按时间点比:dayjs 往返会改字符串形状,按字符串比等于每次都算"改过"
    assert "dayjs(before as string).valueOf()" in page


# ================================================================ C-01 / C-11 交付与冒烟


def test_the_delivery_required_files_exist_and_pack_verifies_them():
    assert (PROJECT_ROOT / ".gitignore").exists(), "根 .gitignore 又丢了(C-01)"
    assert (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").exists(), (
        "CI 配置又丢了 —— 没有执行者的门禁不是门禁(C-01)"
    )
    pack = _read(PROJECT_ROOT / "tools" / "pack.sh")
    assert "REQUIRED=(" in pack and ".github/workflows/ci.yml" in pack, (
        "pack.sh 只查禁品不查必备品 —— 漏打隐藏文件的包会再次'验证通过'发出去"
    )
    assert "'.env.example'" in pack.split("REQUIRED=(", 1)[1].split(")", 1)[0], (
        ".env.example 是 make init 与配置契约的输入,交付包必须复验它存在"
    )
    assert 'zip -q -X "$OUT" "${ENV_EXAMPLES[@]}"' in pack, (
        ".env.* 黑名单会吃掉模板,必须在排除整类后明确补回已知安全模板"
    )
    assert 'frontend/.env.example' in pack.split("REQUIRED=(", 1)[1].split(")", 1)[0], (
        "前端配置模板也会被 .env.* 吃掉,交付包必须复验它存在"
    )
    assert 'grep -vxF -e "${ENV_EXAMPLES[0]}" -e "${ENV_EXAMPLES[1]}"' in pack, (
        "复验必须只豁免两个精确模板路径,不能放过其他 .env.*"
    )

    windows_pack = _read(PROJECT_ROOT / "tools" / "pack.ps1")
    assert "$ForbiddenDirs" in windows_pack and ".secrets" in windows_pack, (
        "Windows 打包脚本必须执行同类禁品排除"
    )
    assert "ZipFile]::OpenRead" in windows_pack, "Windows 打包后必须重新打开产物复验"
    assert "Test-ForbiddenArchivePath" in windows_pack, "Windows 成包复验不能只检查必备文件"
    assert "[System.IO.File]::Delete($Out)" in windows_pack, (
        "Windows 成包发现禁品后必须删包并失败"
    )


def test_packaging_entrypoints_are_pinned_to_lf():
    """用户级 core.autocrlf 不得再次把 Bash 入口检出成 CRLF/混合换行。"""
    attributes = _read(PROJECT_ROOT / ".gitattributes")
    for rule in (
        "*.sh text eol=lf",
        "*.ps1 text eol=lf",
        "Makefile text eol=lf",
    ):
        assert rule in attributes, f".gitattributes 缺少跨平台打包规则: {rule}"

    for relative in ("tools/pack.sh", "tools/pack.ps1", "Makefile"):
        payload = (PROJECT_ROOT / relative).read_bytes()
        assert b"\r" not in payload, f"{relative} 出现 CRLF/混合换行,Windows 打包会复发"


#: 行尾策略的**唯一实现**,从 `tools/normalize_eol.py` 载入。
#:
#: ## 为什么改成载入,而不是在这里再写一份
#:
#: 这条用例原来自己持有跳过表、豁免表和二进制判据 —— 也就是**第二份策略**。
#: 而两个打包脚本现在也要用同一套判据去验成包的真实字节(`tools/pack.sh`
#: 与 `tools/pack.ps1` 的行尾复验),三份手写清单的下场这个仓库已经知道:
#: `pack.sh` 开头两百行讲的就是它,`verify_delivery.py` 里那条逐项比对七个
#: 数组的门禁也是为它而写。
#:
#: 分叉在这里的具体形状是「这条用例绿着,而打出来的包带 CRLF」,或者反过来
#: 「包干净而用例红」—— 两种都属于"门禁看起来在跑"。
#:
#: 从文件路径载入而不是 `import`:`tools/` 在仓库根,不在 `backend` 的包路径里,
#: 而这批用例要在**只有 python3**的机器上跑(`tools/run_pure_tests.py`),
#: 不能靠安装或 PYTHONPATH。载不进来就当场红 —— 策略文件丢了本身就是缺陷,
#: 那时候两个打包脚本会 fail-closed,而这条用例不该反而变绿。
_EOL_TOOL_PATH = PROJECT_ROOT / "tools" / "normalize_eol.py"


def _eol_policy():
    """从**源码字节**载入策略,不走 loader 的 pyc 缓存。

    `exec_module` 底下的 `get_code` 按 (mtime, size) 校验 `__pycache__` ——
    而一次"改了又改回来"的编辑(变异验证正是这么干的)如果发生在同一秒内
    且前后字节数相同,两项都命中,**加载的是改坏的那版**。这条守卫的职责
    恰恰是验证策略文件,验证器自己读缓存等于没验。compile+exec 每次都
    从盘上的字节出发,顺带把"策略文件即真相"这件事写死在加载方式里。
    """
    assert _EOL_TOOL_PATH.exists(), (
        f"载不进行尾策略:{_EOL_TOOL_PATH} —— 两个打包脚本的行尾复验也依赖它"
    )
    module = types.ModuleType("_normalize_eol")
    module.__file__ = str(_EOL_TOOL_PATH)
    code = compile(
        _EOL_TOOL_PATH.read_text(encoding="utf-8"), str(_EOL_TOOL_PATH), "exec"
    )
    exec(code, module.__dict__)  # noqa: S102 - 加载的是仓库内的策略文件本身
    return module


def test_no_tracked_text_file_carries_crlf():
    """**全仓没有 CRLF**,而不只是三个打包入口。

    ## 这条守卫补的是什么

    上面那条只盯 `tools/pack.sh` / `tools/pack.ps1` / `Makefile` 三个文件,
    而 `.gitattributes` 当时也只是一张 5 条模式的白名单 —— 实测
    `git check-attr` 的结果是 814 个跟踪文件里**只有 4 个有规则**。
    其余 810 个跟着打包那台机器的 `core.autocrlf` 走,而 Git for Windows
    默认 `true`。

    于是同一个版本在两台机器上打出来的包内容不同,而**两个打包脚本都没做错**:
    它们逐字节复制工作树。三处会真的坏掉(compose 的 `env_file` 保留 `\\r`
    让密码悄悄变错、Dockerfile 的续行断掉、`.dockerignore` 条目失配),
    理由全文在 `.gitattributes` 顶部。

    ## 为什么扫文件系统而不是问 git

    `eol=lf` 之后 Windows 的工作树也是 LF,所以文件系统扫描在两个平台上
    结论相同 —— 而 `git ls-files` 在纯测试运行器里不保证可用(见 `_looks_binary`)。
    代价是会看到未跟踪的临时文件,所以跳过表要和打包排除表同向。
    """
    found = _eol_policy().offenders(PROJECT_ROOT)

    assert not found, (
        "以下文本文件行尾不合规 —— 打包会把它带进交付物,而 compose 的 env_file、"
        "Dockerfile 续行、.dockerignore 都会因此出错:\n  "
        + "\n  ".join(f"[{kind}] {name}" for name, kind in found[:20])
        + "\n修法:`python3 tools/normalize_eol.py --write .`;仓库里的树还要"
        " `git add --renormalize .` 让索引跟上。"
    )


def test_the_line_ending_policy_has_exactly_one_implementation():
    """**判据不许有第二份。** 这条守着上一条的委托关系不被改回手写。

    上一条原来自己持有跳过表、豁免表和二进制判据;而 `tools/pack.sh` 与
    `tools/pack.ps1` 的行尾复验现在也要用同一套判据去验成包的真实字节。
    三个入口各写一份的话,分叉出来的表现恰好是「Linux 打的包干净、Windows
    打的包带 CRLF」,而两台机器各自跑的时候都显示 `==> OK` —— 那正是这道
    门禁存在的理由。

    两个打包脚本都必须**调用**它,而不是各自实现;并且它必须在两侧的必备
    文件清单里(`REQUIRED` / `$Required`),否则收包方从解出来的树重打时
    会找不到它而 fail-closed。
    """
    assert _EOL_TOOL_PATH.exists(), "行尾策略的唯一实现不见了"
    module = _eol_policy()
    for name in ("offenders", "rewrite", "classify", "normalize_bytes", "SKIP_DIRS"):
        assert hasattr(module, name), f"tools/normalize_eol.py 少了 {name}"

    for script in ("pack.sh", "pack.ps1"):
        body = (PROJECT_ROOT / "tools" / script).read_text(encoding="utf-8")
        assert "normalize_eol.py" in body, (
            f"tools/{script} 没有调用行尾策略 —— 它会逐字节把 CRLF 复制进包,"
            "然后打印 OK"
        )
        assert "--check" in body, f"tools/{script} 没有对成包做行尾复验"
        # **锚定清单行的缩进形状**,不裸搜文件名:两个脚本里 `Join-Path` /
        # `EOL_CHECKER=` 那行也含同一个路径,裸搜会让"从清单里摘掉"仍然绿 ——
        # 变异验证抓到过这一洞(摘掉 $Required 里那份,守卫没红)。
        listed = (
            "\n  'tools/normalize_eol.py'"
            if script == "pack.sh"
            else "\n    'tools/normalize_eol.py'"
        )
        assert listed in body, (
            f"tools/{script} 的必备文件清单里没有 tools/normalize_eol.py —— "
            "收包方从解出来的树重打时会找不到它(门禁会 fail-closed,"
            "而这是一个完全可以避免的 fail-closed)"
        )


def test_the_normalizer_folds_crlf_and_lone_cr_without_inventing_blank_lines():
    """归一化的**顺序**:先 `\r\n` 再 `\r`。反过来会凭空多出空行。

    `b"a\r\nb".replace(b"\r", b"\n")` 得到 `b"a\n\nb"` —— 一次"顺手改一下"
    就能把整棵树的行数翻倍,而 diff 里看起来只是行尾变了。
    """
    module = _eol_policy()
    assert module.normalize_bytes(b"a\r\nb\r\n") == b"a\nb\n", "CRLF 没折干净或折出了空行"
    assert module.normalize_bytes(b"a\rb\r") == b"a\nb\n", "孤立 CR 没折成 LF"
    assert module.normalize_bytes(b"a\nb\n") == b"a\nb\n", "干净的 LF 不许被改动"
    # 混合与孤立 CR 都要被认出来,否则 `.env.example` 那种 331 行里只有 6 行
    # 带 `\r` 的文件会漏网 —— 而那 6 行里有真的变量赋值。
    assert module.classify(b"a\r\nb\n") == "mixed"
    assert module.classify(b"a\rb") == "cr"
    assert module.classify(b"a\r\nb\r\n") == "crlf"
    assert module.classify(b"a\nb\n") == "lf"
    assert module.classify(b"a\x00\r\nb") == "binary"


def test_gitattributes_normalizes_the_whole_tree_not_a_whitelist():
    """**反向断言,窗口封闭。** 兜底规则不许被改回白名单。

    上一条扫的是"今天的树干净不干净",这一条扫的是"明天还会不会干净" ——
    少了兜底,一次 Windows 上的 clone 就能让 810 个文件重新变成 CRLF,
    而那台机器上的工作树看起来完全正常。两条缺一不可。

    兜底必须排在最前:`.gitattributes` 的规则**越靠后越优先**,写在
    `*.bat text eol=crlf` 后面的话会把那条覆盖掉,而 `cmd.exe` 对 LF 的
    `.bat` 行为不可靠。
    """
    lines = [
        line.strip()
        for line in _read(PROJECT_ROOT / ".gitattributes").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, ".gitattributes 里一条规则都没有"
    assert lines[0] == "* text=auto eol=lf", (
        "第一条规则必须是全仓兜底 `* text=auto eol=lf`。"
        f"现在是 {lines[0]!r} —— 白名单只盖得住它点名的那几个文件"
    )
    for suffix in ("*.bat", "*.cmd"):
        assert any(line.startswith(f"{suffix} ") and "eol=crlf" in line for line in lines), (
            f"{suffix} 必须保留 CRLF:cmd.exe 对 LF 结尾的批处理行为不可靠"
        )


def test_make_pack_uses_the_native_windows_entrypoint():
    makefile = _read(PROJECT_ROOT / "Makefile")
    assert "ifeq ($(OS),Windows_NT)" in makefile
    assert (
        "PACK_COMMAND = powershell -NoProfile -ExecutionPolicy Bypass "
        "-File tools/pack.ps1"
    ) in makefile
    assert "PACK_COMMAND = tools/pack.sh" in makefile
    assert "\t$(PACK_COMMAND) $(V)" in makefile


def test_smoke_exercises_the_license_gate_instead_of_the_bypass():
    """冒烟以前不传 model_template_id,任务走 MODEL_REFERENCE 那条已知缝:
    §10.5/§11 四道检查在"端到端"脚本里一次都没被执行过 —— 假绿的具体形状。

    **a48 补一条:光传进去已经不够了。** a47 之后方案会接管
    `model_template_id`,所以「冒烟验的是这个已授权模特」这句话变成了
    **有条件成立** —— 条件是那只 SKU 所属 SPU 上没有 ACTIVE 方案。
    传参那半边不变(不传就是走回绕行缝),但脚本必须**回读出参**并在
    被接管时说出来,否则这条守卫会一直绿着,而它的文档字符串在说一件
    不一定成立的事。判据与"为什么是 note 不是 fail"在 `smoke_test.create_task`
    的函数文档里,结论在 `DECISIONS.md` §3.73 第二节。
    """
    smoke = _read(BACKEND_ROOT / "app" / "scripts" / "smoke_test.py")
    assert "def create_model_template" in smoke
    assert '"license_status": "LICENSED"' in smoke
    assert '"age_verified": "true"' in smoke
    assert '"model_template_id": model_template_id' in smoke, (
        "任务创建又不带模特模板了 —— 冒烟重新走回合规绕行缝(C-11/C-10)"
    )
    assert 'task.get("model_template_id")' in smoke, (
        "回读没了 —— 方案接管时冒烟会拿一个自己都不知道换过的模板继续跑,"
        "而报告仍写着「已授权模特那条闸走过了」"
    )
    assert "由生成方案接管" in smoke, (
        "被接管时不再出声 —— 一次沉默的替换正是 a47 §5 修的那个错位的镜像版本"
    )
    assert "不覆盖" in smoke, "覆盖边界的声明没了 —— 脚本不许再自称完整闭环"


def test_the_model_reference_bypass_is_closed():
    """C-10 已关闭(2026-08-11 评审):自由上传的模特参考图不再放行。

    ## 这条断言换过方向,别照旧版本改回去

    旧版本叫 `..._now_leaves_a_trace`,断言的是那条分支里有一句
    `logger.warning` 然后 `return` —— 也就是**钉着绕行缝本身存在**,只要求
    它出声。当时的理由是"溯源列已落,但自由素材尚未解析到等价授权主体"。

    而 PRD §6.4 明写自由上传模特图不得绕过 ModelTemplate 校验,所以那条
    `return` 是一条与明文要求相反的执行路径,不是一个"还没做的功能"。
    评审给的方向同时写了 fail-closed 兜底,而今天**确实**解析不到授权主体
    (`MediaAsset.consent_id` 全仓没有写入点),所以落点就是拒绝。

    两道门都要钉:创建任务那一道,和 worker 里那条退回自由素材的兜底 ——
    后者更隐蔽,它发生在四道检查都过了之后。
    """
    src = _be("services/generation_service.py")
    branch = re.search(
        r'if "MODEL_REFERENCE" in roles:(.*?)\n        raise ValidationError', src, re.S
    )
    assert branch, "找不到 MODEL_REFERENCE 分支"
    assert "return" not in branch.group(1), (
        "MODEL_REFERENCE 分支又放行了 —— 那条路跳过 §10.5 受众与 §11 "
        "授权/年龄/AI 换装/禁用品类的全部检查(PRD §6.4 明令禁止)"
    )
    assert "logger.warning" in branch.group(1), (
        "拒绝也要出声 —— 人工测试期间「有多少次试图走这条路」要答得上来"
    )

    # 看**代码**不看注释:删掉的那一行以注释形式留在原处(记着它为什么被删),
    # 而按原文搜索会把那段注释算成"它又回来了" —— 第一版就是这么假红的
    worker = _py_code_only(_be("tasks/generation_tasks.py"))
    assert 'model_url = image_reference(by_type["MODEL_REFERENCE"]' not in worker, (
        "worker 又退回自由上传的模特图了 —— 这是同一条缝的第二道门:"
        "创建时模板是好的,执行时模板被停用,于是静默换一张没有授权记录的图把钱花掉"
    )
