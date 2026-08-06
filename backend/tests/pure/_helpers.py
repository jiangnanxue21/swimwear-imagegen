"""纯测试共用工具。刻意不使用 pytest fixture,以便无 pytest 环境也能运行。"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def png_bytes(
    width: int = 800, height: int = 1200, color=(30, 90, 120), mode: str = "RGB"
) -> bytes:
    from PIL import Image

    img = Image.new(mode, (width, height), color if mode != "RGBA" else (*color, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(width: int = 800, height: int = 1200, color=(200, 170, 120)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def expect_raises(exc_type, fn, *args, **kwargs):
    """轻量版 pytest.raises,返回捕获到的异常。"""
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"期望 {exc_type.__name__},实际抛出 {type(e).__name__}: {e}") from e
    raise AssertionError(f"期望抛出 {exc_type.__name__},但没有抛出")


@contextlib.contextmanager
def temporary_secret_key(value: str = "pure-test-signing-key"):
    """在块内借一把主密钥,块外原样还回去。

    ## 为什么要同时钉住密钥目录

    只设 `SETTINGS_SECRET_KEY` 是不够的。真实 pytest 环境里
    `app.core.config.settings` 是模块级单例,它在 import 那一刻就把环境读完了 ——
    之后再改环境变量,`secrets._material()` 从 `settings` 上读到的仍然是空,
    于是它会走 `_read_or_create_key_file()`,**在仓库根生成 `.secrets/.settings.key`**。

    一个测试往交付树里写一把真实主密钥,而且不报错、不留痕 —— 这正是
    「主密钥连着两个交付包出去」那次事故的形状。

    所以这里两个都设:密钥让它走不到生成那条路,密钥目录保证万一走到了,
    文件也落在临时目录里。
    """
    keys = {
        "SETTINGS_SECRET_KEY": value,
        "SETTINGS_KEY_DIR": tempfile.mkdtemp(prefix="pure-test-secrets-"),
    }
    previous = {name: os.environ.get(name) for name in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


@contextlib.contextmanager
def temporary_config(**values):
    """在块内改一项配置,块外原样还回去 —— **两种运行环境里都真的生效**。

    ## 为什么不能只写 `os.environ[name] = ...`

    `providers/_config.provider_setting` 的取值顺序是
    「设置页覆盖 → `app.core.config.settings` → `os.environ`」,
    而第三条只在 `app.core.config` **import 不进来**时才走得到。于是同一段
    「设环境变量 → 断言取值变了」的代码有两种命运:

        零依赖运行器(`tools/run_pure_tests.py`,没有 pydantic)
            → settings 那条 import 失败 → 退到 os.environ → 断言成立,绿
        真实 pytest / CI 的 backend job(装了 dev 依赖)
            → settings 是 import 时构造的单例 → 环境变量根本不被读 → 红

    也就是说这类用例的颜色由「跑它的那台机器装没装 pydantic」决定。
    `temporary_secret_key` 的文档里已经写过这个坑的另一半(主密钥那次事故),
    这里补上配置项这一半:环境变量照设,同时把 `settings` 单例按新环境重建,
    退出时把**原来那个对象**放回去 —— 不是重新构造一个等值的,
    因为别的模块 `from app.core.config import settings` 拿的是对象引用。

    没有 pydantic 时 import 失败,自动退化成只改环境变量,行为与从前一致。
    """
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update({name: str(value) for name, value in values.items()})

    # 被裁剪的运行环境里本来就没有 pydantic,import 不进来是常态而不是故障。
    try:
        from app.core import config as _config
    except Exception:  # noqa: BLE001
        _config = None

    original = getattr(_config, "settings", None) if _config is not None else None
    if _config is not None:
        _config.settings = _config.Settings()

    try:
        yield
    finally:
        if _config is not None:
            _config.settings = original
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def only_line(text: str, prefix: str, *, what: str = "") -> str:
    """**恰好一行**以 `prefix` 开头(允许前导空白),返回那一行。

    这是这个家族的第三个成员,三个各自对应一种「这一段到哪为止」:

        window()       两端都是**地标**(各自唯一)
        braced_block() 终点是**分隔符**,按括号配平
        only_line()    范围就是一行 —— 终点是换行,而换行是最不唯一的分隔符

    写这一批的时候连着撞了两次才想明白第三种存在:拿 `"\n"` 当 `window()`
    的终点会被当场挡回来(它在起点之后出现 82 次),而那不是 `window()`
    太严,是**行**这种范围本来就不该用地标去框。

    「恰好一行」这条同样重要:Makefile 里 `check-offline: test-pure` 出现两次,
    第二处是一句引用旧写法的注释 —— 和 14-13 那条被注释抢先命中的 `),`
    是同一个形状。
    """
    label = what or f"{prefix!r}"
    hits = [ln for ln in text.splitlines() if ln.lstrip().startswith(prefix)]
    if len(hits) != 1:
        raise AssertionError(
            f"以 {prefix!r} 开头的行有 {len(hits)} 行({label})——"
            "0 行是找错了地方,多行多半有一行是注释"
        )
    return hits[0]


def braced_block(text: str, start: str, *, what: str = "") -> str:
    """从一个 `{` 开头的块切到**它自己**的收尾 `}`。按括号配平,不按第一个 `}`。

    `window()` 管的是**地标**(两端各自唯一);花括号块的终点是**分隔符**,
    在文件里必然出现很多次,拿它当地标只会被 `window()` 挡回来。
    而按「第一个 `}`」切在今天恰好对,是因为这个块没有嵌套 ——
    那不是一条能靠的性质:nginx 的 location 里出现一个 `if` 或 `map`
    就多一层括号,而切窄之后**反向断言会静默变真**。

    切不出配平的收尾就抛,不返回一段可能不完整的文本。
    """
    label = what or f"{start!r}"
    count = text.count(start)
    if count != 1:
        raise AssertionError(f"块的起点 {start!r} 出现 {count} 次({label})")
    begin = text.index(start)
    depth = 0
    for i in range(begin, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[begin : i + 1]
    raise AssertionError(f"{label}:找不到配平的收尾 `}}`")


def window(text: str, start: str, end: str | None = None, *, what: str = "") -> str:
    """从源码里切一段,**并且保证这一段真的是你以为的那一段**。

    ## 为什么不能直接 `text[text.index(a) : text.index(b)]`

    这个仓库栽过两次,两次都在**反向断言**上(`assert X not in window`):

        entry.index("),")     登记项的注释里有一句 `(归属外键不存在),` ——
                              `),` 提前命中,窗口在半路上切断
        wired[...][:200]      定长窗口。被切的那一段一变长,窗口就切在半路上

    两次的后果都**不是变红,是变松**:正向断言碰巧还在残段里,而反向断言
    被残段喂成了**平凡真** —— 守卫在某次无关改动的那一刻悄悄不设防,
    并且照样显示 PASS(A45-batch14-13 撞见,见 `DECISIONS.md` §3.29)。

    ## 它保证三件事

        起点标记恰好出现一次      出现两次 = `index()` 挑了哪一个是碰运气
        终点标记恰好出现一次      同上;而且必须在起点之后
        窗口非空                  空窗口让**任何**反向断言平凡为真

    「恰好一次」这条口径和 `tools/audit_anchors.py` 对变异锚点的要求
    是同一条 —— 一个不唯一的锚点,和一个没有锚点是一回事。

    不传 `end` 就切到文件末尾:那是**安全方向**(窗口只会更宽),
    所以不检查终点。
    """
    label = what or f"{start!r}"
    first = text.count(start)
    if first != 1:
        raise AssertionError(
            f"窗口起点 {start!r} 在源码里出现 {first} 次({label})——"
            "不唯一的定位串等于没有定位串,切出来的是哪一段全看运气"
        )
    begin = text.index(start)
    if end is None:
        chunk = text[begin:]
    else:
        after = text[begin:]
        count = after.count(end)
        if count != 1:
            raise AssertionError(
                f"窗口终点 {end!r} 在起点之后出现 {count} 次({label})——"
                "0 次会切到文件尾、多次会提前截断,两种都让反向断言失去意义"
            )
        chunk = after[: after.index(end)]
    # 检查的是**起点标记之后**还有没有东西,不是整段空不空:窗口把标记本身
    # 包在里面,所以「整段为空」永远不成立 —— 那样写出来的是一条不可达的
    # 防御代码,和没写一样(§3.29 第一节,上一批刚踩过)
    if not chunk[len(start) :].strip():
        raise AssertionError(
            f"起点标记之后什么都没有({label})——"
            "这样的窗口让任何 `not in` 断言平凡为真"
        )
    return chunk
