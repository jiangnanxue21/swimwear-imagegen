#!/usr/bin/env python3
"""把散落的 hex 收进 theme.ts 的 brandTokens。

合并方向见 theme.ts 里每个令牌的注释。这里只做机械替换 + 补 import,
不改任何布局。
"""
import pathlib
import re
import sys

SRC = pathlib.Path('src')
SKIP = {SRC / 'theme.ts'}

# hex(大写) -> 令牌名
MAP = {
    '#1B4B5A': 'marine',
    '#E6EFF1': 'marineSoft',
    '#3F6C8F': 'accent',
    '#C8A87C': 'sand',
    '#FFFDF7': 'sandSoft',
    '#16202A': 'ink',
    '#5B6B79': 'slate',
    '#9AA5AD': 'textMuted',
    '#8A98A5': 'textMuted',
    '#A8B4BE': 'textMuted',
    '#BBB': 'textFaint',
    '#3F8F5B': 'success',
    '#3F8F5F': 'success',      # 与 #3F8F5B 差一个字符的绿,合并
    '#C25B3A': 'warning',
    '#D48806': 'warning',      # antd 的金,不属于这套色
    '#CF3B34': 'danger',
    '#C0392B': 'dangerDeep',
    '#B03A3A': 'dangerDeep',
    '#F6F7F8': 'canvas',
    '#FFF': 'surface',
    '#FFFFFF': 'surface',
    '#FAFBFC': 'surfaceAlt',
    '#FAFAFA': 'surfaceAlt',
    '#F0F2F3': 'imageBg',
    '#F5F6F7': 'imageBg',
    '#FDF3F2': 'dangerBg',
    '#FBF2F2': 'dangerBg',
    '#E7EAEC': 'line',
    '#E3E7EA': 'line',
    '#D9DEE2': 'lineStrong',
}

HEX = re.compile(r'#[0-9a-fA-F]{3,8}\b')


def token_for(hex_text: str) -> str | None:
    name = MAP.get(hex_text.upper())
    return f'brandTokens.{name}' if name else None


def replace_single_quoted(source: str) -> tuple[str, int]:
    """处理 '...' 字符串字面量。

    整串就是一个颜色  -> brandTokens.x
    颜色混在别的文字里 -> 转成模板字符串 `1px solid ${brandTokens.x}`
    """
    hits = 0

    def sub(match: re.Match[str]) -> str:
        nonlocal hits
        body = match.group(1)
        if not HEX.search(body):
            return match.group(0)
        exact = token_for(body.strip())
        if exact and body.strip() == body:
            hits += 1
            return exact
        # 混排:逐个替换成 ${...},整串换成反引号
        unknown = False

        def inner(m: re.Match[str]) -> str:
            nonlocal unknown
            tok = token_for(m.group(0))
            if not tok:
                unknown = True
                return m.group(0)
            return '${' + tok + '}'

        rebuilt = HEX.sub(inner, body)
        if unknown:
            return match.group(0)
        hits += 1
        return f'`{rebuilt}`'

    return re.sub(r"'([^'\n]*)'", sub, source), hits


def replace_jsx_attr(source: str) -> tuple[str, int]:
    """JSX 属性上的裸色值:strokeColor="#1B4B5A" -> strokeColor={brandTokens.marine}"""
    hits = 0

    def sub(match: re.Match[str]) -> str:
        nonlocal hits
        tok = token_for(match.group(2))
        if not tok:
            return match.group(0)
        hits += 1
        return f'{match.group(1)}={{{tok}}}'

    return re.sub(r'(\w+)="(#[0-9a-fA-F]{3,8})"', sub, source), hits


def import_line_for(path: pathlib.Path) -> str:
    depth = len(path.relative_to(SRC).parts) - 1
    prefix = './' if depth == 0 else '../' * depth
    return f"import {{ brandTokens }} from '{prefix}theme'"


def insert_import(source: str, path: pathlib.Path) -> str:
    if re.search(r'\bbrandTokens\b', source) is None:
        return source
    if re.search(r"import\s*\{[^}]*\bbrandTokens\b[^}]*\}\s*from", source):
        return source

    lines = source.split('\n')
    last_import_end = -1
    inside = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not inside and stripped.startswith('import '):
            inside = True
        if inside:
            # import 语句以 from '...' 或 import '...' 收尾
            if re.search(r"from\s+'[^']+'\s*$", stripped) or re.fullmatch(r"import\s+'[^']+'", stripped):
                last_import_end = i
                inside = False
    if last_import_end < 0:
        return source
    lines.insert(last_import_end + 1, import_line_for(path))
    return '\n'.join(lines)


def main() -> int:
    total = 0
    touched = []
    for path in sorted(SRC.rglob('*')):
        if path.suffix not in {'.ts', '.tsx'} or path in SKIP or not path.is_file():
            continue
        original = path.read_text()
        text, a = replace_single_quoted(original)
        text, b = replace_jsx_attr(text)
        if a + b == 0:
            continue
        text = insert_import(text, path)
        path.write_text(text)
        total += a + b
        touched.append((str(path), a + b))

    for name, n in touched:
        print(f'  {n:>3}  {name}')
    print(f'\n共替换 {total} 处,涉及 {len(touched)} 个文件')
    return 0


if __name__ == '__main__':
    sys.exit(main())
