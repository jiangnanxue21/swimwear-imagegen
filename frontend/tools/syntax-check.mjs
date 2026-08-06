/**
 * 前端语法体检。
 *
 * 用途:在没有 node_modules 的环境(CI 冷启动、离线机器)里,
 * 只用 TypeScript 编译器做「语法」诊断,不做类型检查也不解析模块。
 * 正式类型检查请用 `npm run typecheck`。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, extname } from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const ts = require('typescript')

const SRC = new URL('../src', import.meta.url).pathname
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
  const rel = file.replace(SRC, 'src')
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
