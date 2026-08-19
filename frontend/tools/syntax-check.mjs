/**
 * 前端离线体检:两遍。
 *
 * 用途:在没有 node_modules 的环境(CI 冷启动、离线机器)里,只用 TypeScript
 * 编译器**自己**做诊断,不解析模块、不做类型检查。正式类型检查仍是
 * `npm run typecheck`。
 *
 *     一  语法       parseDiagnostics —— 文件解析得通吗
 *     二  死 import  声明了却没有任何地方引用的导入名
 *     三  断 import  本地模块里根本没有这个导出名(后端 `verify_imports.py` 的对侧)
 *
 * ## 为什么加第二遍(a48)
 *
 * 第二遍补的是一个**结构性盲区**,不是顺手多验一项。
 *
 * `tsconfig.json` 开着 `noUnusedLocals: true`,所以一个没人用的 import 会让
 * `npm run typecheck` **变红**。而 typecheck 要 node_modules,离线机器上装不了;
 * 于是在这棵树上,死 import 的唯一发现者是「有人恰好在联网机器上跑了一次前端门禁」。
 *
 * 这条缝已经漏过两轮,而且是同一个人在同一轮里**专门去找**都没找全:
 *
 *     a46-phase6  自审时点名找死 import,找到 2 处(ColdStartBanner 的 Space、
 *                 SettingsPage 的 brandVars),**漏了第 3 处**
 *     a47         没动那个文件,离线门禁全绿,而 `AppLayout.tsx` 的
 *                 `WarningOutlined` 从 phase6 起就一直死在那里 ——
 *                 交付出去的树跑不过前端第一道门禁
 *
 * 结论按仓库既有的那条(CLAUDE.md 硬规则 4 第二段)写:**靠人记住的防线会漏**,
 * 而这一类缺陷离线时**没有任何东西在看**。所以补的是机械落点,不是一句叮嘱。
 *
 * ## 为什么走 AST 而不是正则
 *
 * 正则数不清三件事,而三件都会**误报**(误报比漏报贵:一条会冤枉人的门禁
 * 会被下一个人加 `|| true`):
 *
 *     `...AUDIENCES` 展开     前面那个 `.` 会让"单词边界"判定失效
 *     JSX 里的 `<Foo />`      标签名是 Identifier,不在普通表达式位置
 *     只在类型位置用的名字    `const x: Foo` —— 而它同样受 noUnusedLocals 管
 *
 * 反过来,属性名(`obj.Foo`、`{ Foo: 1 }`)**不是**对导入名的引用,所以那两处
 * 位置上的 Identifier 要排掉 —— 不排的话 `theme.Space` 会让一个真的死了的
 * `Space` 看起来还活着。
 *
 * ## 覆盖 src 与 tests 两棵子树
 *
 * `tsconfig.json` 的 `include` 是 `["src", "tests"]`,所以 tests 下的死 import
 * 同样会让 typecheck 变红。原来这个脚本只走 `src`,于是"离线门禁绿了"这句话
 * 在 tests 上从来没成立过。
 *
 * ## 为什么加第三遍(a48,同批)
 *
 * 后端的 `backend/tools/verify_imports.py` 早就在 `check-offline` 里,做的是
 * 「`app.*` 的 import 是否都指向真实存在的东西(零依赖)」。**前端一直没有对侧。**
 * 于是「把一个导出改名 / 删掉,漏了某个调用点」这类改动在离线时同样无人看管 ——
 * 而 a46-phase6 与 a47 恰恰各做过一次横跨九个文件的删除(Token 存储链、
 * `adminHeaders`、`usingAdminFallback`、`sharedActor`)。这一次没漏,
 * 但"这一次没漏"和"有东西在看"是两件事。
 *
 * 只管**本地模块**(`./`、`../`、`@/`)。第三方包要 node_modules 才知道它导出什么,
 * 那正是这个脚本不做的事 —— 越界去猜会得到一堆误报。
 *
 * 遇到 `export * from '...'` 直接放行:再往下追要做完整的模块图,
 * 而收益是几个边角。宁可漏,不可冤。
 *
 * ## 三遍都验不到什么
 *
 * 死变量、死参数、类型、渲染行为,以及第三方包的导入对不对。
 * 别把它读成"typecheck 可以不跑了" —— 它只让「离线全绿」这句话少骗人一点。
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, extname, dirname, sep } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const ts = require('typescript')

/**
 * `fileURLToPath` 而不是 `.pathname`。
 *
 * `new URL(...).pathname` 给的是 **URL 的路径分量**,不是文件系统路径。
 * 两处会错:
 *
 *     空格等字符仍是百分号转义   /tmp/space%20dir/src      -> ENOENT
 *     Windows 盘符前多一个斜杠   /D:/source%20code/...     -> D:\D:\source%20code\...
 *
 * 后者正是这个脚本在 `D:\source code\swimwear-imagegen` 下的表现:
 * `join()` 拿到一个以 `/` 开头的串,当成"当前盘根目录起算的相对路径"再拼一次盘符。
 * 两处都只在**带空格的路径**或 **Windows** 上出现,所以 CI(Linux、无空格)
 * 一直是绿的 —— 这条门禁在开发机上从来没跑通过,而在 CI 上从来没失败过。
 */
const ROOTS = [
  ['src', fileURLToPath(new URL('../src', import.meta.url))],
  ['tests', fileURLToPath(new URL('../tests', import.meta.url))],
]
const EXTS = new Set(['.ts', '.tsx'])

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

/**
 * 这个文件里声明的导入名 -> 声明它的行号。
 *
 * 四种形态都要认,漏掉哪一种都是**漏报**:
 *
 *     import X from 'm'             默认导入
 *     import { A, B as C } from 'm'  具名(含改名 —— 记的是改名**之后**那个)
 *     import * as NS from 'm'       命名空间
 *     import type { T } from 'm'    仅类型(noUnusedLocals 一样管它)
 *
 * `import 'm'` 没有绑定名,天然不参与。
 */
function declaredImports(source) {
  const out = new Map()
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue
    const line = source.getLineAndCharacterOfPosition(statement.getStart(source)).line + 1
    const clause = statement.importClause
    if (clause.name) out.set(clause.name.text, line)
    const bindings = clause.namedBindings
    if (!bindings) continue
    if (ts.isNamespaceImport(bindings)) {
      out.set(bindings.name.text, line)
    } else {
      for (const element of bindings.elements) out.set(element.name.text, line)
    }
  }
  return out
}

/**
 * 除 import 声明之外,文件里出现过的每一个标识符。
 *
 * 四处位置上的 Identifier **不是**对某个绑定的引用,必须排掉:
 *
 *     obj.Foo          属性访问的 `.name`
 *     { Foo: 1 }       非简写属性名 —— 简写 `{ Foo }` 是引用,不能排
 *     { Foo: T }       接口里的属性签名名
 *     NS.Foo           限定名的右半边
 *
 * 其余一律算引用,包括 JSX 标签、类型位置、`export { Foo }`。宁可算宽:
 * 算宽了是漏报(联网机器上 typecheck 仍会兜住),算窄了是误报 ——
 * 而一条会冤枉正常文件的门禁,活不过第一次被它拦住的人。
 */
function referencedNames(source) {
  const seen = new Set()
  const visit = (node) => {
    if (ts.isImportDeclaration(node)) return
    if (ts.isIdentifier(node)) {
      const parent = node.parent
      const isPropertyName =
        (ts.isPropertyAccessExpression(parent) && parent.name === node) ||
        (ts.isPropertyAssignment(parent) && parent.name === node) ||
        (ts.isPropertySignature(parent) && parent.name === node) ||
        (ts.isQualifiedName(parent) && parent.right === node)
      if (!isPropertyName) seen.add(node.text)
    }
    ts.forEachChild(node, visit)
  }
  ts.forEachChild(source, visit)
  return seen
}

/**
 * 一个本地模块对外给出的名字。`export *` 用一个哨兵表示"追不动,放行"。
 */
function exportedNames(source) {
  const names = new Set()
  for (const s of source.statements) {
    const mods = (ts.getModifiers ? ts.getModifiers(s) : s.modifiers) ?? []
    const isExported = mods.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)
    const isDefault = mods.some((m) => m.kind === ts.SyntaxKind.DefaultKeyword)
    if (isExported) {
      if (isDefault) names.add('default')
      if (ts.isVariableStatement(s)) {
        for (const d of s.declarationList.declarations) {
          if (d.name && d.name.text) names.add(d.name.text)
        }
      } else if (s.name && s.name.text) {
        names.add(s.name.text)
      }
    }
    if (ts.isExportDeclaration(s)) {
      if (s.exportClause && ts.isNamedExports(s.exportClause)) {
        for (const e of s.exportClause.elements) names.add(e.name.text)
      } else if (!s.exportClause) {
        names.add(STAR) // export * from '...' —— 见文件头「宁可漏,不可冤」
      }
    }
    if (ts.isExportAssignment(s)) names.add('default')
  }
  return names
}
const STAR = '\u0000export-star'

/**
 * `./x` / `../x` / `@/x` -> 磁盘文件。
 *
 * 返回 `null` 表示第三方包(不管),`undefined` 表示本地模块但文件找不到。
 * `@/` 的映射来自 `tsconfig.json` 的 paths,改那里要一起改这里 ——
 * 这是本文件唯一一处重复了 tsconfig 的配置。
 */
function resolveLocal(fromFile, spec, srcRoot) {
  if (!spec.startsWith('.') && !spec.startsWith('@/')) return null
  const base = spec.startsWith('@/')
    ? join(srcRoot, spec.slice(2))
    : join(dirname(fromFile), spec)
  for (const c of [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) {
    if (existsSync(c) && statSync(c).isFile() && EXTS.has(extname(c))) return c
  }
  return undefined
}

let parseErrors = 0
let deadImports = 0
let brokenImports = 0
let checked = 0

const SRC_ROOT = ROOTS[0][1]
const exportCache = new Map()
const sourceOf = (file) =>
  ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.ES2020,
    true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )

/** 第三遍:每个具名/默认导入,目标模块里真的有吗 */
function brokenImportsIn(source, file) {
  const out = []
  for (const s of source.statements) {
    if (!ts.isImportDeclaration(s) || !s.importClause) continue
    const spec = s.moduleSpecifier.text
    const target = resolveLocal(file, spec, SRC_ROOT)
    if (target === null) continue
    const line = source.getLineAndCharacterOfPosition(s.getStart(source)).line + 1
    if (target === undefined) {
      out.push([line, `本地模块 ${spec} 找不到对应文件`])
      continue
    }
    if (!exportCache.has(target)) exportCache.set(target, exportedNames(sourceOf(target)))
    const has = exportCache.get(target)
    if (has.has(STAR)) continue
    const wanted = []
    if (s.importClause.name) wanted.push(['default', s.importClause.name.text])
    const nb = s.importClause.namedBindings
    if (nb && ts.isNamedImports(nb)) {
      for (const e of nb.elements) wanted.push([(e.propertyName ?? e.name).text, e.name.text])
    }
    for (const [original, local] of wanted) {
      if (!has.has(original)) {
        const alias = original === local ? '' : `(as ${local})`
        out.push([line, `${spec} 没有导出 ${original}${alias}`])
      }
    }
  }
  return out
}

for (const [label, root] of ROOTS) {
  for (const file of walk(root).filter((f) => EXTS.has(extname(f)))) {
    checked += 1
    const source = sourceOf(file)
    // 输出统一用 `/`:Windows 上 `join()` 给的是反斜杠,不归一化的话
    // 同一次失败在两个平台上是两行不同的文本,没法直接比对
    const rel = file.replace(root, label).split(sep).join('/')

    const diags = source.parseDiagnostics ?? []
    if (diags.length > 0) {
      parseErrors += diags.length
      for (const d of diags) {
        const { line, character } = source.getLineAndCharacterOfPosition(d.start ?? 0)
        console.log(
          `  FAIL ${rel}:${line + 1}:${character + 1} ` +
            ts.flattenDiagnosticMessageText(d.messageText, ' '),
        )
      }
      // 解析不通时不再查 import:那一份 AST 本来就是残的,报出来的都是噪音
      continue
    }

    const dead = []
    const referenced = referencedNames(source)
    for (const [name, line] of declaredImports(source)) {
      if (!referenced.has(name)) dead.push([name, line])
    }
    const broken = brokenImportsIn(source, file)

    if (dead.length === 0 && broken.length === 0) {
      console.log(`  PASS ${rel}`)
      continue
    }
    deadImports += dead.length
    brokenImports += broken.length
    for (const [name, line] of dead) {
      console.log(`  DEAD ${rel}:${line} 导入了 ${name},文件里没有任何地方用它`)
    }
    for (const [line, message] of broken) {
      console.log(`  BROKEN ${rel}:${line} ${message}`)
    }
  }
}

// ---------------------------------------------------------------- 四  调色板 token
//
// a61 新增。`brandVars` 是 `Object.fromEntries(...)` 出来的
// `Record<BrandToken, string>` —— **取一个不存在的键得到 `undefined`,而 CSS
// 收到 undefined 只是不上色**。类型上 TS 会拦(键是 `keyof typeof lightTokens`),
// 但这台机器跑不了 typecheck,而 a60 当轮就写出过 `brandVars.successBg` /
// `brandVars.dangerSoft` 两个不存在的键 —— 它们不报错、不变红,只是那一格
// 没有背景色,而"这里本来该有颜色"没有人记得。
//
// 判据是 `theme.ts` 里 `lightTokens` 的键。那份表是唯一事实来源
// (`brandVars` 与暗色表都从它派生),所以这里读它而不是维护第二份清单。
function paletteTokens(themeSource) {
  const start = themeSource.indexOf('const lightTokens')
  if (start < 0) return null
  const block = themeSource.slice(start)
  const end = block.indexOf('\n}')
  if (end < 0) return null
  return new Set(
    [...block.slice(0, end).matchAll(/^ {2}([a-zA-Z]+):/gm)].map((m) => m[1]),
  )
}

let badTokens = 0
{
  const themePath = join(SRC_ROOT, 'theme.ts')
  const tokens = paletteTokens(readFileSync(themePath, 'utf-8'))
  if (tokens === null || tokens.size === 0) {
    // 读不出来就**红**,不是静静跳过 —— 静静跳过等于这一格自己变成一句空话
    console.log('  BROKEN src/theme.ts 解析不出 lightTokens,调色板检查失去了判据')
    badTokens += 1
  } else {
    const allFiles = ROOTS.flatMap(([, root]) =>
      walk(root).filter((f) => EXTS.has(extname(f))),
    )
    for (const file of allFiles) {
      const rel = file.slice(file.indexOf(`${sep}frontend${sep}`) + 10) || file
      if (rel.endsWith('theme.ts')) continue
      const text = readFileSync(file, 'utf-8')
      for (const m of text.matchAll(/brandVars\.([a-zA-Z]+)/g)) {
        if (!tokens.has(m[1])) {
          const line = text.slice(0, m.index).split('\n').length
          console.log(
            `  TOKEN ${rel}:${line} brandVars.${m[1]} 不在调色板里 —— 取到 undefined,那一格不会上色`,
          )
          badTokens += 1
        }
      }
    }
  }
}

const failures = parseErrors + deadImports + brokenImports + badTokens
console.log(
  `\n${checked - failures}/${checked} files clean  (语法 + 死 import + 断 import + 调色板 token)`,
)
if (deadImports > 0) {
  console.log(
    `\n死 import ${deadImports} 处。tsconfig 开着 noUnusedLocals,` +
      '所以它们在联网机器上会让 `npm run typecheck` 直接变红 —— 删掉,别加豁免。',
  )
}
if (brokenImports > 0) {
  console.log(
    `\n断 import ${brokenImports} 处。这一类 typecheck 一样会红,` +
      '而它多半意味着某处改名/删除漏了调用点 —— 先去看那个模块最近改了什么。',
  )
}
if (badTokens > 0) {
  console.log(
    `\n调色板 token ${badTokens} 处。这一类**运行时完全静默** —— ` +
      'undefined 传给 CSS 只是不上色,而"这里本来该有颜色"没有人记得。',
  )
}
process.exit(failures ? 1 : 0)
