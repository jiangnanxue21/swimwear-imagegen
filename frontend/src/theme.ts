import type { ThemeConfig } from 'antd'

/**
 * 主题令牌:深海军蓝 + 沙色。
 * 选它的理由很实在——这是泳装影棚的工作台,界面里最大面积的东西是商品图和候选图,
 * 主色必须让位给图片。所以饱和度压低、只在操作项上出现,不做任何渐变或强调色块。
 *
 * ## 这份表原本只存在于纸面上
 *
 * 走查发现:全仓只有 `AppLayout` 一个文件 import 过 `brandTokens`,另外 37 个组件里
 * 散着 165 处写死的 hex,其中 `#5B6B79` 被手抄了 62 遍。手抄抄出了漂移——
 * `#3F8F5B` 和 `#3F8F5F` 是两个差一个字符的绿,`#CF3B34` / `#c0392b` / `#B03A3A`
 * 是三个红,`#9AA5AD` / `#8A98A5` / `#A8B4BE` / `#bbb` 是四个灰。没人打算要这些。
 * 现在全部收进来了,并在收的过程中把漂移合并掉(合并方向见每条注释)。
 *
 * ## 为什么 CSS 也走这里
 *
 * `global.css` 不能 import TS。原本的做法是两个文件各写一份 hex——那正是漂移的
 * 来源。所以改成 `applyBrandCssVars()` 在启动时把令牌写进 `:root`,CSS 侧用
 * `var(--brand-slate, #5B6B79)` 引用(带 fallback,JS 跑起来之前不会裸奔)。
 * 单一事实来源仍然是这个文件。
 */
export const lightTokens = {
  // ---- 外框 -------------------------------------------------------------
  /** 顶栏底色。**不等于 `ink`** —— ink 是正文色,暗色模式下它会翻成浅色 */
  headerBg: '#16202A',
  /** 压在 headerBg 上的字。两个模式下都是白的,所以它不参与翻转 */
  onHeader: '#FFFFFF',

  // ---- 品牌 -------------------------------------------------------------
  marine: '#1B4B5A',
  marineSoft: '#E6EFF1',
  /** 比 marine 亮一档,用在「选中 / 主图」这类需要压过图片的描边上 */
  accent: '#3F6C8F',
  sand: '#C8A87C',
  /** 沙色的极浅底,配 sand 描边(提示性区块) */
  sandSoft: '#FFFDF7',

  // ---- 文字 -------------------------------------------------------------
  ink: '#16202A',
  slate: '#5B6B79',
  /** 次要说明文字。合并了原来的 #9AA5AD / #8A98A5 / #A8B4BE */
  textMuted: '#9AA5AD',
  /** 占位符「—」「0」这类。合并了原来的 #bbb,换到同一套蓝灰阶 */
  textFaint: '#B3BCC3',

  // ---- 语义 -------------------------------------------------------------
  /** 合并了 #3F8F5B / #3F8F5F 这对差一个字符的绿 */
  success: '#3F8F5B',
  /** 需人工 / 待确认。合并了原来的 #D48806(那是 antd 的金,不属于这套色) */
  warning: '#C25B3A',
  /** 阻断 / 失败 */
  danger: '#CF3B34',
  /** 红字压在浅红底上时用,比 danger 深一档。合并了 #c0392b / #B03A3A */
  dangerDeep: '#B03A3A',

  // ---- 面 ---------------------------------------------------------------
  canvas: '#F6F7F8',
  surface: '#FFFFFF',
  /** 表头、次级区块底。合并了 #FAFBFC / #fafafa */
  surfaceAlt: '#FAFBFC',
  /** 图片位的底色(contain 之后的留白)。合并了 #F0F2F3 / #F5F6F7 */
  imageBg: '#F0F2F3',
  /**
   * 分段控件(Segmented)的轨道底。**必须比 canvas 深一档,不能等于它。**
   *
   * antd 的 Segmented 默认拿 `colorBgLayout` 当轨道色,而这套主题把
   * `colorBgLayout` 设成了 canvas —— 内容区的背景也是 canvas,于是轨道
   * 和页面底色**逐字节相同**,控件没有任何边界。表现见 `buildTheme` 里
   * Segmented 那一段。
   *
   * 不复用 `imageBg`:那个值在暗色下是刻意的中性灰(为了不干扰运营判断
   * 泳装颜色),拿来当控件底会在一片偏蓝的界面里泛灰。
   * 也不复用 `surfaceAlt`:它比 canvas **更浅**(#FAFBFC vs #F6F7F8),
   * 用它做轨道等于让控件比页面更亮,方向反了。
   */
  track: '#EBEEF1',
  /** 阻断行、错误块的浅红底。合并了 #FDF3F2 / #FBF2F2 */
  dangerBg: '#FDF3F2',

  // ---- 线 ---------------------------------------------------------------
  /** 卡片、图块描边。合并了 #E7EAEC / #E3E7EA */
  line: '#E7EAEC',
  lineSoft: '#F0F2F3',
  /** 需要比 line 更明确一点的分隔(如非阻断问题的行首竖条) */
  lineStrong: '#D9DEE2',
} as const

export type BrandToken = keyof typeof lightTokens

/**
 * 暗色调色板。**键必须与 `lightTokens` 完全一致** —— 下面的 `assertSameKeys`
 * 在开发环境里当场校验,漏一个键的后果是那处颜色在暗色下保持亮色,
 * 而这种漏网通常要等到某个人在深夜的仓库里盯着一块刺眼的白才被发现。
 *
 * ## 不是把亮色反相
 *
 * 反相会毁掉这套配色存在的理由:主色让位给图片。暗色下重新定的三条规矩 ——
 *
 *   **语义色提亮、降饱和。** `#CF3B34` 那种红压在深底上会震(同等亮度差下红色
 *   在暗底上的视觉锐度远高于亮底),所以红/绿/橙都往浅里挪一档、把饱和度收一点。
 *
 *   **面与线拉开的是明度,不是饱和度。** canvas / surface / surfaceAlt 三层只差
 *   4~6 点明度,靠 `line` 分隔。暗色里最常见的错误是把层与层的差值放大到能一眼
 *   看出,结果整个界面变成一堆浮起来的方块。
 *
 *   **图片底色不跟着变蓝。** 见 `imageBg` 那条注释,这是这套界面里唯一一个
 *   「暗色不能只按美观来定」的值。
 */
export const darkTokens: Record<BrandToken, string> = {
  // ---- 外框 ----
  headerBg: '#0E1317',
  onHeader: '#FFFFFF',

  // ---- 品牌 ----
  marine: '#5FA8C0',
  marineSoft: '#16333D',
  accent: '#7FB3D5',
  sand: '#D9BE95',
  sandSoft: '#26211A',

  // ---- 文字 ----
  ink: '#E6EBEF',
  slate: '#9FB0BC',
  textMuted: '#7D8C97',
  textFaint: '#5E6B75',

  // ---- 语义 ----
  success: '#5CB37D',
  warning: '#E08B6A',
  danger: '#F0736C',
  /** 亮色下 dangerDeep 比 danger 深一档;暗色下「更强调」= 更亮,所以方向反过来 */
  dangerDeep: '#FF9A94',

  // ---- 面 ----
  canvas: '#12171C',
  surface: '#1A2026',
  surfaceAlt: '#212930',
  /**
   * 图片位的底色。**这是整份暗色表里唯一一个不按美观定的值。**
   *
   * 这一页的运营动作是判断泳装的颜色、面料和印花对不对。图块用 contain,
   * 所以每张图周围都有一圈底色,而人眼判断颜色是相对的 —— 带蓝调的底会把米色
   * 读成偏暖,带暖调的底会把白色读成发黄。所以这里刻意用**中性灰**
   * (R≈G≈B),不跟着界面其余部分往蓝里偏。
   *
   * 明度也不能太低:纯黑底会让深色泳装的轮廓消失,而「背面拍糊了」这类判断
   * 依赖的正是轮廓。取在中间偏暗,和亮色模式的 #F0F2F3 大致对称。
   */
  imageBg: '#2C2E30',
  /** 暗色下「深一档」的方向反过来:轨道要比 canvas **亮**才看得见 */
  track: '#232A31',
  dangerBg: '#3A2321',

  // ---- 线 ----
  line: '#2E363D',
  lineSoft: '#262D33',
  lineStrong: '#3C444B',
}

export type ThemeMode = 'light' | 'dark'

export const palettes: Record<ThemeMode, Record<BrandToken, string>> = {
  light: lightTokens,
  dark: darkTokens,
}

/**
 * 组件里用的令牌:值是 `var(--brand-*)` 而不是 hex。
 *
 * ## 这一层是暗色模式能在一天内落地的全部原因
 *
 * 走查时全仓有 172 处 `brandTokens.X`,散在 33 个文件的 inline style 里。
 * 如果它们拿到的是 hex,暗色模式就得让这 33 个文件都去 subscribe 一个
 * React context,再逐个改成运行时取值 —— 那是个几百行、会漏掉一半的改动。
 *
 * 而它们拿到的如果是 `var(--brand-slate)`,切换主题就只是改 `:root` 上的
 * 二十几个变量:浏览器负责把所有引用重算一遍,React 一次都不用重渲染。
 * `global.css` 早就是这么引的(上一轮为了消除色值漂移做的),这里只是让 TS 侧
 * 用上同一个机制。
 *
 * 单一事实来源仍然是 `lightTokens` / `darkTokens` —— 这个对象是从它的键推出来的,
 * 不是手抄的。
 */
export const brandVars = Object.fromEntries(
  Object.keys(lightTokens).map((name) => [name, `var(--brand-${kebab(name)})`]),
) as Record<BrandToken, string>

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)
}

/**
 * 图片位尺寸。
 *
 * 走查里最贵的一条不是「不好看」,是「看不清」:这是一套图片生产系统,运营的核心
 * 动作是判断图好不好,而界面把图压得比它能承受的更小。15 个硬错误码里相当一部分
 * 是手指、面料、水印这类细节,150px 高看不出来。
 *
 * 尺寸也做成令牌而不是每页写个数字:改的时候要一起改,否则同一张候选图在审核页和
 * 任务页是两个大小,运营会以为看到的是两张图。
 */
export const imageTile = {
  /** 审核/判定用。要能看出手指、面料、水印 */
  review: 300,
  /** 浏览用(素材库、商品详情、模特模板) */
  gallery: 220,
  /** 编排用(拖拽排序时至少要认得出是哪张) */
  compact: 96,
  /** 列表行内缩略图 */
  row: 44,
  /** 详情页页眉缩略图 */
  header: 64,
} as const

/** `.asset-grid` 每格的最小宽度,跟着 tile 高度走 */
export const gridMin = {
  review: 260,
  gallery: 190,
} as const

/**
 * 字号档位。
 *
 * ## 为什么补这一张表
 *
 * 上一轮把 antd 的基准从 11 抬到了 13(理由见 `buildTheme`:运营要盯 8 小时,
 * 中文在 11px 下笔画会糊)。但那次只改了基准,**没改 inline style** ——
 * 走查时全仓仍有 72 处手写 `fontSize: 12` 把次要文字压了回去,
 * 而次要文字恰恰是 Tooltip 说明、问题描述、卡片 hint 这类**要读进去**的内容。
 * 抬档只做了一半。
 *
 * 分档的判据是一句话:**这句话是要读的,还是要扫的?**
 *
 *     要读的   hint、问题描述、Tooltip 正文、校验提示   -> body(13)
 *     要扫的   时间戳、`v3`、来源标注、单位            -> meta(12)
 *
 * 12px 没有被删掉,它仍然是合法档位 —— 删掉的是「随手写 12」。
 */
export const fontScale = {
  /** 元信息:时间戳、版本号、来源、单位。一眼扫过的内容才用它 */
  meta: 12,
  /** 正文与次要说明。**默认用它** */
  body: 13,
  /** 强调正文:当前步骤、关键字段 */
  strong: 15,
  /** 页面标题(PageHeader) */
  title: 18,
  /**
   * 卡片数字。
   *
   * 走查 M-2 合并:原本 20 与 22 并存 —— 导入页/批量页的 Statistic 和工作台的
   * 计数块用 20,FlowHeader 的两个计数块用 22。两者是**同一个语义**
   * (一格里的那个大数),没有理由差 2px。按用量合并到 20。
   */
  metric: 20,
  /** 首页大数字。比 metric 大一档,只给「今日工作」的待办卡片 */
  metricLg: 26,
} as const

/**
 * 间距档位。收敛走查时发现的 `gap: 6 / 8 / 10 / 12 / 14` 五个值。
 * 6 和 10 没有存在的理由 —— 它们不是设计决定,是当时手边的数字。
 */
export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 } as const

/**
 * 圆角两档。走查时 `3` 出现 4 次、`4` 出现 6 次,而主题里写的是 4 ——
 * 三处事实来源。现在明确:
 *
 *     sm   图片、缩略图这类**内嵌**在卡片里的元素
 *     md   卡片、按钮、输入框等**容器**级元素(与 antd token.borderRadius 一致)
 */
export const radius = { sm: 3, md: 4 } as const

/** 内容区最大宽度(M-5)。2560 屏上一行铺到底时,中文超过 45 字就很难回行 */
export const layoutMax = 1680

/**
 * 把令牌写进 `:root`,供 `global.css` 与 `brandVars` 用。
 * 名字规则:`--brand-<驼峰转短横>`,例如 `slate` -> `--brand-slate`、
 * `dangerBg` -> `--brand-danger-bg`。
 *
 * 切换主题就是再调一次它。不需要卸载上一套变量:两份调色板的键完全一致,
 * 后写的把前面的覆盖掉。
 */
export function applyBrandCssVars(
  mode: ThemeMode = 'light',
  root: HTMLElement = document.documentElement,
): void {
  for (const [name, value] of Object.entries(palettes[mode])) {
    root.style.setProperty(`--brand-${kebab(name)}`, value)
  }
  root.style.setProperty('--tile-review', `${imageTile.review}px`)
  root.style.setProperty('--tile-gallery', `${imageTile.gallery}px`)
  root.style.setProperty('--tile-compact', `${imageTile.compact}px`)
  root.style.setProperty('--grid-min-review', `${gridMin.review}px`)
  root.style.setProperty('--grid-min-gallery', `${gridMin.gallery}px`)

  // 告诉浏览器该用哪套原生控件配色。漏了它的话,滚动条、日期选择器、
  // 表单自动填充的底色仍然是亮的 —— 暗色界面里那几块白最扎眼,
  // 而且是 CSS 变量管不到的地方
  root.style.colorScheme = mode
  root.dataset.theme = mode
}

/** 两份调色板的键必须一一对应。漏一个键 = 那处颜色在暗色下保持亮色。 */
export function assertSameKeys(): void {
  const light = Object.keys(lightTokens).sort()
  const dark = Object.keys(darkTokens).sort()
  if (light.join() !== dark.join()) {
    throw new Error(
      `调色板键不一致:亮色缺 ${dark.filter((k) => !light.includes(k))},` +
        `暗色缺 ${light.filter((k) => !dark.includes(k))}`,
    )
  }
}

export function buildTheme(mode: ThemeMode, algorithms: {
  defaultAlgorithm: ThemeConfig['algorithm']
  darkAlgorithm: ThemeConfig['algorithm']
}): ThemeConfig {
  // antd 组件内部的颜色由算法生成,不能喂 `var(...)` —— 它要拿真值去算
  // hover / active / disabled 的派生色。所以这里用调色板的原值,
  // 而组件 inline style 用 brandVars。两边同源,不会漂移。
  const t = palettes[mode]
  return {
    algorithm: mode === 'dark' ? algorithms.darkAlgorithm : algorithms.defaultAlgorithm,
    token: {
      colorPrimary: t.marine,
      colorLink: t.marine,
      colorTextBase: t.ink,
      colorBgLayout: t.canvas,
      colorBgContainer: t.surface,
      colorBorderSecondary: t.line,
      colorSuccess: t.success,
      colorWarning: t.warning,
      colorError: t.danger,
      // **不要删这一行。** antd 的 `colorInfo` 是一个**独立的种子令牌**,
      // 设 `colorPrimary` 不会带上它 —— 漏了它的后果是 `<Tag color="processing">`
      // 和 `<Alert type="info">`(冷启动横幅就是 info)全部渲染成 antd 默认的
      // #1677ff,而那个蓝和 marine / accent 挨在一起时是三个不同的蓝。
      // 这属于 M-1 那一类漂移里最隐蔽的一处:它不在任何一处代码里写着 "blue"
      colorInfo: t.marine,
      borderRadius: 4,
      // 走查:`fontSize: 11` 在代码里出现了 62 次,11px 才是这个界面的**事实正文字号**,
      // 而运营要盯 8 小时,中文在 11px 下笔画会糊。整体上抬一档(11->12、12->13),
      // 密度损失用行高换回来。
      fontSize: 13,
      lineHeight: 1.6,
      /*
       * 字体栈。**这一行原来的首位是 `'Inter'`,而 Inter 从来没有被加载过。**
       *
       * `index.html` 里没有任何 `<link>`,全仓没有 `@font-face`,
       * `package.json` 里没有任何字体依赖。所以 Inter 这一档在**大多数机器上
       * 直接落空**,而在少数装了它的机器上(设计同事、部分 Linux 发行版)会命中。
       * 后果是拉丁字形由操作系统随机决定:同一颗「按 SKU」按钮,
       * 有人看到的 SKU 是 Inter、有人是 Segoe UI、有人是 SF —— 而中文永远落到
       * PingFang / 雅黑。中西文搭配本来就要调,现在连搭配的是谁都不确定。
       *
       * 改法是**声明我们真的有的东西**:系统字体优先,中文各平台各给一档,
       * 末尾补 `Noto Sans SC` 兜住 Linux 与容器(无头浏览器截图、Docker 里跑的
       * e2e 都在那条路径上,原来的栈在那里会掉到 DejaVu Sans,中文变方框)。
       *
       * 要真用 Inter 的话得先把它装进来(`@fontsource-variable/inter` + 一行
       * import),那是一个独立决定 —— 但**不能维持现状**:栈里写着一个不存在的
       * 名字,等于把排版结果交给了运营那台机器上恰好装过什么。
       */
      fontFamily:
        "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', "
        + "'Microsoft YaHei', 'Noto Sans SC', sans-serif",
      wireframe: false,
    },
    components: {
      // 顶栏两个模式下都是深色:它是这套界面的定位标识,跟着翻会让人以为换了系统
      Layout: { headerBg: t.headerBg, headerHeight: 52, siderBg: t.surface },
      Menu: { itemSelectedBg: t.marineSoft, itemSelectedColor: t.marine },
      Table: { headerBg: t.surfaceAlt, cellPaddingBlockSM: 8 },
      Card: { paddingLG: 16 },
      /*
       * 分段控件的轨道必须看得见。
       *
       * antd 的 `Segmented.trackBg` 默认取 `colorBgLayout`,而上面把
       * `colorBgLayout` 设成了 `t.canvas` —— 内容区背景也是 canvas。
       * 两者相同的后果在工作台页头那一排上最明显(截图取样确认过:
       * 轨道区域的像素值与页面底色完全一致):
       *
       *     控件没有边界   「按 SKU / 按款」看起来是一块白片加两个灰字,
       *                    不像一个可以切的开关;未选中的那一项读起来像
       *                    说明文字或禁用态
       *     高度掉一档     按钮是 controlHeightSM(24),而 Segmented 只剩
       *                    选中片可见,那是 24 - trackPadding×2 = 20。
       *                    一行里因此出现 24 / 20 / 24 三个视觉高度
       *     两组糊成一组   「按 SKU|按款」和「紧凑|展开」是两个独立开关,
       *                    轨道都不可见时它们连成「四个并列选项」
       *
       * 三条都不是字号问题 —— 那一排的字号实测全是 13px,和 token 一致。
       * 补上轨道之后高度自动对齐到 24,不需要再动尺寸。
       */
      Segmented: { trackBg: t.track, itemSelectedBg: t.surface },
    },
  }
}
