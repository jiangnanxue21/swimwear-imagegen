/**
 * 前端语法体检。
 *
 * 用途:在没有 node_modules 的环境(CI 冷启动、离线机器)里,
 * 只用 TypeScript 编译器做「语法」诊断,不做类型检查也不解析模块。
 * 正式类型检查请用 `npm run typecheck`。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, extname, sep } from 'node:path'
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
const SRC = fileURLToPath(new URL('../src', import.meta.url))
const EXTS = new Set(['.ts', '.tsx'])

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    return statSync(full).isDirectory() ? walk(full) : [full]
  })
}

const files = walk(SRC).filter((f) => EXTS.has(extname(f)))
let errors = 0

for (const file of files) {
  const source = ts.createSourceFile(
    file,
    readFileSync(file, 'utf8'),
    ts.ScriptTarget.ES2020,
    true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  const diags = source.parseDiagnostics ?? []
  // 输出统一用 `/`:Windows 上 `join()` 给的是反斜杠,不归一化的话
  // 同一次失败在两个平台上是两行不同的文本,没法直接比对
  const rel = file.replace(SRC, 'src').split(sep).join('/')
  if (diags.length === 0) {
    console.log(`  PASS ${rel}`)
  } else {
    errors += diags.length
    for (const d of diags) {
      const { line, character } = source.getLineAndCharacterOfPosition(d.start ?? 0)
      console.log(`  FAIL ${rel}:${line + 1}:${character + 1} ${ts.flattenDiagnosticMessageText(d.messageText, ' ')}`)
    }
  }
}

console.log(`\n${files.length - errors}/${files.length} files parsed cleanly`)
process.exit(errors ? 1 : 0)
