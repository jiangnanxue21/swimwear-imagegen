"""a69 守卫:`AppError.message` 是**界面文案**,不是日志。

## 来路:一条约定成立了,但没有任何东西在看着它

`api/client.ts` 的 `describeError` 对业务 4xx 的处理写着:

    后端的 message 就是写给运营的话,原样透出

这条约定本身是对的 —— 判定只有一份,前端不许再翻译一次。但它有一个后果
没人守着:**后端每写一条 `message`,都是在写界面文案**,而全仓 250 多条里
没有任何门禁在看它们像不像人话。

`core/errors.py` 的 `AppError` 文档其实早就写了半条规矩:

    ``message`` 会返回给客户端,因此禁止在其中拼接数据库异常原文

写下了,但只是一句注释。a69 走查抓到的实例正是它想禁的那一类:

```
受众取值不合法:'KIDS' is not a valid Audience     StrEnum 的原生英文报错
属性值不合字段定义 —— garment_type: …             机器字段名,而中文名一直在注册表里
不是合法取值:'花色';允许的是 ['FLORAL', 'SOLID']   Python 的引号与列表字面量
SPU {spu_id} 不存在                                运营认识款号,不认识 UUID
请先用 POST /spus 建档                             运营界面上没有这个东西
```

**共同形状不是"文案没润色",是「把内部表示直接当成人话交出去」。**
而每一条的正确做法在同一个仓库里都已经存在:`AUDIENCE_LABELS`、
`AttributeField.label`、`spu_code`、款式列表页的「新建款式」按钮。
不是没想到,是没铺满。

## 射程:只守形状,不评价文风

这条守卫**不**判断一句话写得好不好 —— 那件事自动化判不了,也不该由门禁判。
它只认四种可机检的泄漏:

    A  把异常对象拼进 message           f"…{exc}" / {e} / {err}
    B  让运营去调 API                    POST /xxx、GET /xxx、"端点"
    C  回显内部 id                       {*_id} / {*.id}(UUID 对运营没有意义)
    D  Python 语法进了文案               {x!r} 的引号、sorted(...) 的列表字面量

四种都有明确的替代写法,所以判红之后下一步是清楚的 —— 这是它敢当门禁的前提。

## 例外表怎么用

`_ALLOWED` 里每一条都必须写理由。会写进去的只有一种情况:**这条报错的读者
本来就不是运营**(运维清理接口、只有改 URL 参数才触发的排序校验)。
给那些写运营话术是把文案改得更模糊,不是更清楚。

例外表只能凭理由增长,不能凭"改起来麻烦"。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
APP = PROJECT / "backend" / "app"

#: 这些异常的 message 会原样出现在运营眼前(`describeError` 的业务 4xx 那一支)
_USER_FACING = {
    "ValidationError",
    "ConflictError",
    "NotFoundError",
    "StateError",
    "ForbiddenError",
    "DuplicateError",
}

#: 读者本来就不是运营的报错。**每一条都要有理由**,只能凭理由增长
_ALLOWED: dict[str, str] = {
}


#: **第二类出口:不是 `AppError` 子类,但文案照样会到运营眼前。**
#:
#: 第一版只认 `_USER_FACING` 那六个类,于是漏了三条同类泄漏(自查时才发现):
#:
#:     AttributeValueError(spec.name, f"…{raw!r}")   reason 被接口层拼进 422 的 message
#:     FilterRejected(f"…{text!r}")                  api/ai_tests.py 转成 400 的 message
#:     AudienceGateResult(problems=(f"…{x!r}",))     service.py 拼进"受众校验未通过:…"
#:
#: 共同点:它们本身不是 HTTP 错误,但**下游有一个已知的调用点把它们的文本
#: 原样交给运营**。判据因此不是"它是不是异常",是"它的文本会不会被透出"。
#:
#: 加一条新出口之前先问:这个东西的文本有没有一条路通到界面上。有就加进来。
_RELAYED = {
    # 类名: 文案在第几个位置参数(有的第一个参数是字段名)
    "AttributeValueError": 1,
    "FilterRejected": 0,
    "AudienceGateResult": None,  # 关键字 problems=,单独处理
}


def _messages() -> list[tuple[str, int, str]]:
    """把所有面向用户的报错 message 摘出来(文件, 行号, 文案)。

    只取文案那一个参数 —— `code=` / `http_status=` / `detail=` 都不是文案。
    f-string 保留占位符原样(`{exc}` 这种正是要抓的东西)。
    """
    out: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(PROJECT / "backend"))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)

            # 第二类出口:转发型
            if name in _RELAYED:
                idx = _RELAYED[name]
                if idx is None:
                    for kw in node.keywords:
                        if kw.arg != "problems":
                            continue
                        for elt in getattr(kw.value, "elts", []):
                            try:
                                out.append((rel, node.lineno, ast.unparse(elt)))
                            except Exception:  # noqa: BLE001
                                pass
                    continue
                if len(node.args) > idx:
                    try:
                        out.append((rel, node.lineno, ast.unparse(node.args[idx])))
                    except Exception:  # noqa: BLE001
                        pass
                continue

            if name not in _USER_FACING or not node.args:
                continue
            first = node.args[0]
            # 相邻字符串字面量会被解析成一个 JoinedStr / Constant,
            # `ast.unparse` 把它还原成源码形状 —— 占位符因此留得住
            try:
                text = ast.unparse(first)
            except Exception:  # noqa: BLE001
                continue
            out.append((rel, node.lineno, text))
    return out


def _live() -> list[tuple[str, int, str]]:
    return [m for m in _messages() if m[0] not in _ALLOWED]


def test_there_are_messages_to_check_at_all():
    """先证明这条门禁有对象。

    改了异常类名、换了构造方式、`_USER_FACING` 写漏一个 —— 三种都会让它
    变成一句空话而没有任何征兆。**什么都没查的门禁和全都过了的门禁,
    在 CI 上长得一模一样。**
    """
    msgs = _messages()
    assert len(msgs) > 150, (
        f"只摘出 {len(msgs)} 条面向用户的报错 —— 解析多半失效了,而失效时它是绿的"
    )


def test_no_message_interpolates_an_exception_object():
    """A:异常对象不许进 message。

    `AppError` 自己的文档写着「禁止在其中拼接数据库异常原文」。这里把那句话
    变成一条会红的规则,并且不止于"数据库":`StrEnum` 的
    `'KIDS' is not a valid Audience` 同样是 Python 写给 Python 的话。

    要那段信息就放 `detail`(只进日志),运营那句自己写。
    """
    bad = [
        m for m in _live()
        if re.search(r"\{\s*(exc|e|err|error|ex)\s*(?:!r|!s|![ra])?\s*\}", m[2])
    ]
    assert not bad, "把异常对象拼进了运营看的那句话:\n" + "\n".join(
        f"  {file}:{line}\n      {text[:120]}" for file, line, text in bad
    )


def test_no_message_tells_an_operator_to_call_an_endpoint():
    """B:不许指挥运营去调接口。

    运营界面上没有「POST /spus」,对应的是款式列表页那个按钮。报错要指的是
    **他点得到的那个动作** —— 说一个他找不到的东西,等于没说。
    """
    bad = [
        m for m in _live()
        if re.search(r"\b(POST|GET|PATCH|PUT|DELETE)\s+/|\bapi/v?\d|端点", m[2])
    ]
    assert not bad, "让运营去调 API:\n" + "\n".join(
        f"  {file}:{line}\n      {text[:120]}" for file, line, text in bad
    )


def test_no_message_echoes_an_internal_id():
    """C:不许把内部 id 回显给运营。

    运营认识的是款号 `SW-014`,不是 `a3f2e1c8-…`。**而且找不到那条记录时
    根本查不出码** —— 所以正确做法不是"换成码",是这句话里压根不提它:
    id 放 `detail`,人话里说清下一步该干什么。

    `{spu_code}` / `{sku}` 这类业务码不在此列:那些正是运营认识的东西。
    """
    bad = [
        m for m in _live()
        if re.search(r"\{\s*\w*(?:_id|_uuid)\b[^}]*\}|\{\s*\w+\.id\s*\}", m[2])
    ]
    assert not bad, "把内部 id 回显给了运营:\n" + "\n".join(
        f"  {file}:{line}\n      {text[:120]}" for file, line, text in bad
    )


def test_no_message_leaks_python_syntax():
    """D:Python 的语法不许进文案。

    `{text!r}` 渲染成 `'花色'`(带 Python 的引号),`sorted(xs)` 渲染成
    `['FLORAL', 'SOLID']`(列表字面量)。两者都在告诉运营一件事:
    你看到的是一个程序的内部状态,不是一句话。

    列可选值是对的 —— 只说"不合法"等于让人挨个试。用 `、`.join(...) 就行。
    """
    bad = []
    for file, line, text in _live():
        if re.search(r"\{[^{}]*![ra][^{}]*\}", text):
            bad.append((file, line, text, "!r / !a 会带出 Python 的引号"))
        elif re.search(r"\{\s*(?:sorted|list|tuple|set|repr)\s*\(", text):
            bad.append((file, line, text, "容器/repr 直接进文案,会渲染成 Python 字面量"))
    assert not bad, "Python 语法进了运营看的那句话:\n" + "\n".join(
        f"  {file}:{line}  ({why})\n      {text[:120]}"
        for file, line, text, why in bad
    )


def test_every_exemption_says_why():
    """例外表只能凭理由增长。

    一条没有理由的例外,和把整条门禁删掉的区别只是时间。
    """
    for path, reason in _ALLOWED.items():
        assert (PROJECT / "backend" / path).exists(), (
            f"例外表里的 {path} 已经不存在 —— 例外该跟着一起删"
        )
        assert len(reason) >= 12, f"{path} 的例外没写清楚理由"


def test_every_exempted_file_still_has_messages_to_exempt():
    """例外必须**还在**指着一个真实的对象。

    ## 这条的第一版判错了方向

    第一版问的是「这个文件里还有没有 A~D 四类泄漏」,答案是没有 —— 于是它
    报「豁免可以删掉」。**而豁免这两个文件的理由从来不是那四类。**
    它们被豁免是因为读者不是运营(`sorting` 只有手改 URL 参数才走得到,
    `cleanup_service` 是运维接口),那种理由和文案里有没有 `{exc}` 无关。

    换句话说:第一版把「为什么豁免」和「豁免挡住了什么」混成了一件事。
    记下来是因为这正是这个仓库反复踩的形状 —— 守卫验的东西和它自称验的
    东西不是一回事,而它是绿的。

    现在只守一件能守得住的事:**豁免不许指向一个已经没有对象的文件。**
    文件删了、报错搬走了、异常类换了,三种都会让豁免变成一行死代码,
    而死掉的豁免会在下一次有人往那个路径写新报错时悄悄放行。
    """
    stale = [
        path for path in _ALLOWED
        if not any(m[0] == path for m in _messages())
    ]
    assert not stale, (
        "这些豁免已经没有对象了,删掉它们 —— 留着会在有人往那个路径写新报错时悄悄放行:\n  "
        + "\n  ".join(stale)
    )
