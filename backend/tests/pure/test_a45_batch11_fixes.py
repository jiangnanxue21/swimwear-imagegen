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


def test_the_model_reference_bypass_now_leaves_a_trace():
    """C-10 仍未关闭:溯源列已落,但自由素材尚未解析到等价授权主体。

    在闭环落地之前,每次走缝必须留下结构化日志 —— "这批图有没有过授权闸"
    要答得上来。
    """
    src = _be("services/generation_service.py")
    branch = re.search(
        r'if "MODEL_REFERENCE" in roles:(.*?)return', src, re.S
    )
    assert branch, "找不到 MODEL_REFERENCE 分支"
    assert "logger.warning" in branch.group(1), (
        "绕行缝的告警没了 —— 已知缺口可以存在,不可以无声"
    )
