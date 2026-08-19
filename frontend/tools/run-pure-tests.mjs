#!/usr/bin/env node
/**
 * 前端纯逻辑测试运行器(a61)。零依赖:不需要 node_modules。
 *
 * 用法:node tools/run-pure-tests.mjs [文件名过滤]
 *
 * ## 它补的是一个躺了六轮的洞
 *
 * `make fe-check`(typecheck / lint / Vitest / build)四层全部要 node_modules,
 * 而交付机上装不上。于是从 a54 起,「下一步第一件事就是 make fe-check」在交接
 * 文档里重复了六遍 —— **而每一轮都在往那条没有行为验证的路上多写几个文件**。
 * 到 a60,一轮就改了六个前端文件,全部只过源码形状守卫。
 *
 * 一句重复六遍的「这是环境问题」正在变成免责声明。Node 22 原生剥类型 +
 * `node:test` 把这件事的成本降到零:不装依赖、不转译、不引框架。
 *
 * ## 射程:判定层,不含渲染
 *
 * 剥类型**不做 JSX 变换**,所以 `.tsx` 一行都跑不了。能跑的是 `.ts` 里的纯函数,
 * 而那恰好是"算错了不会有任何征兆"的那一半。组件渲染、hooks、交互仍然只有
 * Vitest 覆盖得了 —— **这件事把欠账从"全部"缩到"渲染那一半",不是把它还清**,
 * 别把这条命令的绿读成 `fe-check` 的绿。
 *
 * ## 发现不到任何用例时**红**,不是静静通过
 *
 * 这条是本仓反复记的那一类:一个什么都没跑的门禁,和一个全都过了的门禁,
 * 在 CI 上长得一模一样。改测试目录、改命名约定、glob 写错,三种都会让它
 * 变成一句空话,而没有任何征兆。
 *
 * ## A67-ISSUE-003:剥类型这件事,以前是**指望**,现在是**声明**
 *
 * 上面那句「Node 22 原生剥类型」写在注释里,而 spawn 出去的命令是光秃秃的
 * `node --test`。这两件事在不同的 Node 22 小版本上结论相反:
 *
 *     Node 22.16.0    node --test x.ts   ERR_UNKNOWN_FILE_EXTENSION   0/2
 *     Node 22.22.2    node --test x.ts   26/26 passed
 *
 * 默认剥类型是 **22.18.0** 才落地的(22.6.0 起有 `--experimental-strip-types`)。
 * 于是一个号称「零依赖、可重复」的门禁,在两台都装着 Node 22 的机器上一红
 * 一绿 —— 而红的那台看到的是一句关于文件扩展名的报错,和测试内容毫无关系。
 * 一条自己都不可重复的门禁,会把所有人对 `check-offline` 结果的信任一起带走。
 *
 * 修法是把运行时前提**编码进来**,而不是继续写在注释里指望它成立:
 *
 *     1. 能力探测       `--experimental-strip-types` 允许就显式带上。
 *                       它在 22.18+ 是多余的,但无害;在 22.6~22.17 是必需的。
 *     2. 版本下限       低于 22.6.0 直接红,并说清楚原因 —— 那台机器**跑不了**
 *                       这个门禁,而不是「测试挂了」。
 *     3. 认领那句报错   万一仍然撞上 ERR_UNKNOWN_FILE_EXTENSION,把它翻译成
 *                       运行时诊断,别让下一个人再去读一遍 diff 才明白。
 *
 * 同一件事的另外两半在仓库别处:`package.json` 的 `engines.node` 写下同一个
 * 下限,`.github/workflows/ci.yml` 的 gates job 显式 `actions/setup-node` ——
 * 那个 job 原来直接跑这个脚本却没有 setup-node,等于把门禁行为托付给
 * GitHub runner 镜像**当天**预装的是哪个小版本。
 */
import { spawnSync } from 'node:child_process'
import { readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const ROOT = join(FRONTEND, 'tests', 'pure')
const SUFFIX = '.test.ts'

/**
 * 能跑这个门禁的最低 Node。**和 `package.json` 的 `engines.node` 是同一个数**,
 * 改一处就得改另一处(`test_a68_fe_pure_runtime.py` 盯着这件事)。
 *
 * 22.6.0 = `--experimental-strip-types` 落地的版本。低于它,`.ts` 进不了
 * `node:test`,而报出来的是一句关于文件扩展名的话 —— 那句话不该由下一个人
 * 自己去解码。
 */
const MIN_NODE = [22, 6, 0]

function parseVersion(raw) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(raw)
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null
}

function isBelow(actual, min) {
  for (let i = 0; i < 3; i += 1) {
    if (actual[i] !== min[i]) return actual[i] < min[i]
  }
  return false
}

const current = parseVersion(process.version)
if (current && isBelow(current, MIN_NODE)) {
  console.error(
    `\x1b[31m这台机器的 Node 是 ${process.version},跑不了这个门禁(至少要 v${MIN_NODE.join('.')})\x1b[0m`,
  )
  console.error('  原因不在测试里:更早的 Node 不会剥 TypeScript 类型,`.ts` 根本进不了 node:test。')
  console.error('  这是**环境不满足**,不是用例失败 —— 换 Node 版本,别去改用例。')
  process.exit(1)
}

/*
 * 22.18.0 起默认剥类型,这个 flag 成了多余的;22.6~22.17 它是必需的。
 * 两种情况下都显式带上,让「跑没跑得起来」不再取决于小版本号。
 *
 * 探测而不是硬写死,是为了将来这个 flag 被移除时不至于变成 `bad option`:
 * 到那时剥类型早已是默认行为,不带它一样跑得起来。
 */
const STRIP_FLAG = '--experimental-strip-types'
const nodeArgs = process.allowedNodeEnvironmentFlags.has(STRIP_FLAG)
  ? [STRIP_FLAG, '--test']
  : ['--test']

function collect(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...collect(full))
    else if (entry.endsWith(SUFFIX)) out.push(full)
  }
  return out
}

let files = []
try {
  files = collect(ROOT)
} catch {
  console.error(`\x1b[31m找不到 ${relative(FRONTEND, ROOT)}/ —— 这个运行器失去了对象\x1b[0m`)
  process.exit(1)
}

const filter = process.argv[2]
if (filter) files = files.filter((f) => f.includes(filter))

if (files.length === 0) {
  console.error(
    `\x1b[31m一个用例文件都没找到(${relative(FRONTEND, ROOT)}/**/*${SUFFIX})\x1b[0m`,
  )
  console.error('  什么都没跑的门禁和全都过了的门禁,在 CI 上长得一模一样。')
  process.exit(1)
}

// `--test` 直接收目录会当成 CJS 模块去 require,报 "Cannot find module"。
// 显式列文件 —— 而且这样才知道到底跑了几个文件。
const proc = spawnSync(process.execPath, [...nodeArgs, ...files], {
  cwd: FRONTEND,
  encoding: 'utf-8',
})
const output = `${proc.stdout || ''}${proc.stderr || ''}`

// 兜底诊断。上面的版本闸已经拦掉了已知的那一种,这里认领的是**没预料到的**
// 那一种:某个 runtime 既没被版本闸拦下,又确实剥不了类型。把它翻译成一句
// 关于运行时的话,而不是让它以「0/2 passed」的形状出现在门禁摘要里 ——
// 后者会让人去查用例,而用例一个字都没错。
if (output.includes('ERR_UNKNOWN_FILE_EXTENSION')) {
  console.error(
    `\x1b[31m这个 Node(${process.version})没有剥掉 TypeScript 类型,用例文件根本没被加载\x1b[0m`,
  )
  console.error(`  已尝试的参数:${nodeArgs.join(' ')}`)
  console.error('  用例本身没有失败 —— 失败的是运行时前提。先确认 Node 版本与 strip-types 支持。')
  process.exit(1)
}

for (const line of output.split('\n')) {
  if (/^ok \d+ - /.test(line)) console.log(`  \x1b[32mPASS\x1b[0m ${line.replace(/^ok \d+ - /, '')}`)
  else if (/^not ok \d+ - /.test(line)) console.log(`  \x1b[31mFAIL\x1b[0m ${line.replace(/^not ok \d+ - /, '')}`)
}

const pass = Number((output.match(/^# pass (\d+)$/m) || [])[1] ?? 0)
const fail = Number((output.match(/^# fail (\d+)$/m) || [])[1] ?? 0)

console.log('')
if (fail > 0) {
  console.log(output.split('\n').filter((l) => l.includes('error:') || l.includes('expected')).slice(0, 12).join('\n'))
}
console.log(
  fail > 0
    ? `\x1b[31m${pass}/${pass + fail} passed, ${fail} failed\x1b[0m  (${files.length} 个文件)`
    : `\x1b[32m${pass}/${pass} passed, 0 failed\x1b[0m  (${files.length} 个文件)`,
)
console.log(
  '\x1b[2m    只覆盖判定层 —— 剥类型不做 JSX 变换,组件渲染仍然只有 Vitest 验得了\x1b[0m',
)
process.exit(proc.status === 0 ? 0 : 1)
