/**
 * ESLint 配置(A7)。
 *
 * ## 这个仓库为什么需要 ESLint
 *
 * 先说不需要的部分:`tsconfig.json` 已经开了 `strict`、`noUnusedLocals`、
 * `noUnusedParameters`、`noFallthroughCasesInSwitch`。未使用变量、隐式 any、
 * switch 漏 break 这些,`npm run typecheck` **已经会拦**。再让 ESLint 报一遍
 * 只会得到两份重复的输出,而且它那份看到的信息还更少。
 *
 * 真正的增量只有一处:**react-hooks**。依赖数组写错、条件里调 Hook,
 * TypeScript 完全看不见——它们在类型上是合法的,只在运行时表现为
 * "偶尔不刷新"和"状态错乱",而那正是最难在 UAT 里复现的一类缺陷。
 * 这个仓库的 Hook 依赖数组基本是手写的,所以这条增量不小。
 *
 * 所以这份配置的取舍是:类型能管的交给类型,ESLint 只做 tsc 做不到的事。
 *
 * ## exhaustive-deps 已经升成 error(前端评审 P2-3)
 *
 * **这一段原来论证的是"为什么它是 warn"** —— 现有代码里有刻意偏离规则的
 * 写法(`WorkbenchReviewPage` 用一个拼接串表达"任一队列的数据新鲜度变了",
 * 规则不认这种复杂表达式),而一上来设成 error 的结果不是这些地方被修好,
 * 是整个 lint 在第一天就被关掉。那个判断在当时是对的。
 *
 * 它的问题在于**后半句一直没有兑现**:"等 Gate A 之后逐个消化,再决定是否
 * 升级"。多轮批次过去,升级没有发生,而 `npm run lint` 当时也没有
 * `--max-warnings=0` —— 于是 warn 在 CI 里等于日志噪音,16 个含手写依赖
 * 数组的文件实际处于**无保护状态**。而这条规则正是本仓上 ESLint 的
 * 唯一真实增量(见上一段):它管的那类缺陷是"偶尔不刷新、状态错乱",
 * UAT 里最难复现的那一种。
 *
 * 一条永远停在 warn 的规则和没有这条规则是一样的,只是更让人放心。
 *
 * 升级时做的三件事:
 *
 *     1. 真能修的就修   `GenerationPlanPanel.openPreview` 改成 useCallback ——
 *                       它原来是每次渲染新建的函数声明,进依赖会让 memo 失效、
 *                       不进则永远警告,是个死结,拆开就没了
 *     2. 复杂表达式提名  `queues.map(...).join(',')` 提成 `queuesFreshness`,
 *                       规则从此看得懂它
 *     3. 剩下的写明理由  两处**刻意**偏离(CopyTab 只在版本变时重置本地编辑、
 *                       queue 的 memo 不能吃每次新建的 queues 数组)改成
 *                       显式 `eslint-disable-next-line` + 一段说明
 *
 * 第 3 条才是升级的意义:**让"我知道我在违反它"和"我没看见这条警告"
 * 在代码里长得不一样。** 前者进 code review,后者滚进日志。
 *
 * `npm run lint` 同时加了 `--max-warnings=0` —— 少了它,error 之外新长出来的
 * warning 又会回到没人读的状态,这次升级半年后就白做了。
 *
 * ## 为什么手动注册 react-hooks 而不用它的预设
 *
 * 该插件在不同大版本里导出的扁平配置入口改过名字(`configs.recommended`
 * 与 `flat` / `recommended-latest` 并存过)。手动注册插件、显式写两条规则,
 * 在哪个版本上都成立,而且读配置的人一眼看得见到底开了什么。
 * 预设省下的两行不值得换来一次"升级依赖后配置静默失效"。
 */
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // 构建产物与依赖不参与。package-lock.json 不是源码
    ignores: ['dist/**', 'node_modules/**', 'coverage/**'],
  },

  // ---------------------------------------------------------------- 前端源码
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      sourceType: 'module',
      globals: globals.browser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      // —— 这份配置存在的理由,见文件头 ——
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',

      // —— 关掉与 tsconfig 重复的那些 ——
      // `noUnusedLocals` / `noUnusedParameters` 已经在 typecheck 里报了,
      // 而且 tsc 认得类型层面的使用(仅作为类型引用的导入),ESLint 不认
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
      // `strict` 下隐式 any 已经是类型错误;显式写 `any` 是有意为之,
      // 全仓库只有一处,值不回它带来的噪音
      '@typescript-eslint/no-explicit-any': 'off',

      // —— tsc 看不见、又确实会出事的几条 ——
      // `if (x = 1)` 这类笔误,类型上完全合法
      'no-cond-assign': ['error', 'always'],
      // 只在开发时打日志。error / warn 留着,它们是有意的
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      // 空的 catch 块会把异常吞掉,而这个仓库的错误处理是靠 `readError` 上浮的
      'no-empty': ['error', { allowEmptyCatch: false }],

      // —— 走查 P1-4 / M-1:两条防回潮规则 ——
      //
      // 这两类问题的共同点是 tsc 和人眼都抓不住:它们不是错误,是**慢慢渗回来**
      // 的东西。上一轮 theme.ts 那段注释记着「165 处硬编码、`#5B6B79` 手抄 62 遍」
      // 的教训 —— 清理一次不难,难的是半年后还干净。所以清理和门禁一起进。
      'no-restricted-syntax': [
        'error',
        {
          // 内部文档编号不进用户可见文案。运营手里没有那份任务书,
          // 「§6.3.2 要求这个数字为 0」对他等于「这里有个数字应该是 0,原因保密」,
          // 而且它同时暗示这句话很重要 —— 于是他会去问开发,
          // 一次本可以不发生的打扰。结论留下,编号搬进注释
          selector:
            "JSXAttribute[name.name=/^(title|message|description|placeholder|extra|label|okText|cancelText)$/] Literal[value=/§\\d|阶段 [1-9]|Gate A|FE-\\d{3}|UAT/]",
          message:
            '内部文档编号不要出现在用户可见文案里(运营没有那份文档)。把结论留下,编号搬到注释。',
        },
        {
          // antd 的调色板预设色**不受 theme.ts 控制**,改一百遍令牌也动不了它们。
          // 语义档(error/warning/success/processing/default)可以用,它们走
          // theme.token.colorXxx;调色板档必须走 BrandTag
          selector:
            "JSXAttribute[name.name='color'] Literal[value=/^(blue|green|orange|gold|volcano|cyan|purple|magenta|geekblue|lime|pink|red|yellow)$/]",
          message:
            'antd 预设调色板色不受 theme.ts 控制,会造成色值漂移。用 <BrandTag tone="..."> 或 brandVars。',
        },
      ],
    },
  },

  // ---------------------------------------------------------------- 测试代码
  //
  // 接上 Vitest 之前,`tests/` 既不过 tsc 也不过 ESLint —— 那 29 条用例是
  // 这个仓库里唯一一批没有任何工具看过的代码,而它们恰好是用来看别人的。
  // 现在两道都补上。
  //
  // 与 `src/**` 的差别只有两处,都是环境差异不是标准放松:
  //   1. 全局对象要同时有 browser(jsdom 用例)和 node(纯函数用例、setup)
  //   2. 主题色那两条防回潮规则不适用 —— 用例里出现 'blue' 是在断言别人不许用它
  {
    files: ['tests/**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
      'no-cond-assign': ['error', 'always'],
      'no-empty': ['error', { allowEmptyCatch: false }],
      'no-console': 'off',
    },
  },

  // ---------------------------------------------------------------- 构建脚本
  //
  // `tools/` 下是 Node 脚本,不是浏览器代码——全局对象是 process / console
  // 而不是 window。它们也要过一遍:`syntax-check.mjs` 本身就是门禁的一部分,
  // 门禁自己写错了没人会发现
  {
    files: ['tools/**/*.{js,mjs}', '*.config.{js,mjs}'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: globals.node,
    },
    rules: {
      'no-console': 'off',
    },
  },
)
