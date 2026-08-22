/**
 * 图片集标签(FE-211~214)。
 *
 * ## 隔离素材不可被选中(FE-211 的验收结果)
 *
 * 隔离态素材在选择区里**显示但禁选**,并且注明原因。不显示的话运营会以为
 * 图丢了,能选的话会一路走到批准才被规则拦下 —— 在选的那一刻就说清楚,
 * 比在批准那一刻报错省一轮返工。
 *
 * ## 已批准的集不原地改
 *
 * 已批准集只提供「基于这一版新建」(`derive`)。原地改会让「平台驳回的是
 * 哪一版图」失去答案,而那是驳回回流第一件要查的事。
 *
 * 拖拽用浏览器原生 HTML5 DnD,不引第三方拖拽库 —— 为一个五到十行的列表
 * 加一个依赖不值得。上下移动按钮同时保留:原生拖拽对键盘用户不可用。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Checkbox, Empty, Image, Select, Skeleton, Space, Tag, Tooltip,
} from 'antd'
import {
  ArrowDownOutlined, ArrowUpOutlined, CopyOutlined, DeleteOutlined, PlusOutlined,
  StarFilled, StarOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MEDIA_STATUS_LABEL, mediaApi, type MediaAsset, type MediaRole } from '../../api/media'
import { IMAGE_ANGLE_LABEL, type ImageAngle } from '../../api/generationPlans'
import {
  imageSetsApi,
  type ImageSetDetail,
  type ImageSetItemInput,
  type VariantCoverage,
} from '../../api/imageSets'
import { isResultUnknown, readWriteError } from '../../api/client'
import { IMAGE_SET_STATUS_LABEL, type FlowStepResult } from '../../api/workbench'
import { IssueList, RejectNotice } from './FlowBits'
import { MEDIA_ROLE_LABEL } from './materialUtils'
import ErrorNotice from '../ErrorNotice'
import UnsavedGuard from '../UnsavedGuard'
import { brandVars, fontScale, imageTile } from '../../theme'
import { formatDateTime } from '../../utils/datetime'
import BrandTag from '../BrandTag'

interface Draft extends ImageSetItemInput {
  /**
   * 本地行标识。**必须和后端的唯一键同口径**(A-25)。
   *
   * 原来这里是裸 `media_asset_id`,注释写着"素材 id 在一个集里唯一"——
   * 而后端的唯一键是 `(image_set_id, media_asset_id, variant_id)`,
   * 且 `0016` 那条唯一索引对 NULL 走 `COALESCE(variant_id, '')`。
   * 也就是说**同一张素材绑两个不同变体是合法数据**。
   *
   * 那种数据一进来,前一版会同时出两个问题:React 重复 key,以及
   * `remove/setPrimary/setRole` 按 key 过滤时**一次命中两行** ——
   * 删一行删掉两行,而两行长得一模一样,运营只会以为自己点重了。
   *
   * A-26 之前前端造不出这种数据(变体恒为 null),经 API 可以;
   * A-26 之后前端自己就能造。所以这两条必须同批上线。
   *
   * 归一口径 `variant_id ?? ''` 与 `schemas/image_set.py` 的
   * `item.variant_id or ""` 逐字对应,也和那条表达式索引一致。
   */
  key: string
}

/**
 * 一次拉多少张素材来建 URL 索引。**抽成常量是因为界面要把这个数说出来**
 * (见 `assetsTruncated` 那个格子的提示)。
 *
 * 写死在请求里的话,调大它就会留下一句说错数的提示 —— 而那句提示恰恰是
 * 为了消掉"对着真实存在的图说它可能已被删除"才加的。
 *
 * 200 与后端 `api/media_library.py` 的 `page_size: le=200` 对齐,不能再大。
 */
const MEDIA_PAGE_SIZE = 200

/** 行标识。三处(`toDraft` / `add` / `setVariant`)必须用同一个函数拼。 */
function rowKey(mediaAssetId: string, variantId: string | null | undefined): string {
  return `${mediaAssetId}#${variantId ?? ''}`
}

/**
 * 变体在界面上的名字。**永远不要直接显示 key。**
 *
 * key 是变体身份(`products.variant_key`),取值是分配那一刻的颜色名。
 * 运营改过一次颜色文案之后,key 就是一个历史值 —— 直接打到界面上,
 * 他会看到自己刚改掉的旧名字,并且合理地认为"没保存成功"再改一次。
 *
 * `labels` 里查不到时才回落到 key:那说明这个标签指向的变体已经不在了
 * (商品被删或换了 SPU),此时显示原始字符串比显示空白有用。
 */
function nameOfVariant(coverage: VariantCoverage, key: string): string {
  return coverage.labels[key] ?? key
}

/**
 * 撞名时可分辨的显示名(A45-#23)。
 *
 * "同名冲突"的定义就是 label 相同,所以横幅原来那句
 * `labelCollisions.map(nameOfVariant).join('、')` 渲染出来是「红色、红色」——
 * 一条提示两个变体同名,而它自己也分不开这两个。
 *
 * 下拉在 A-26 里已经做过这件事(缀 key 尾六位),但那是**内联写的** ——
 * 于是同一个页面里下拉分得开、横幅分不开。抽成一个函数两处共用,
 * 否则这就是第三处"两份实现"(见 A45-#4 的 saveBlob)。
 */
function distinctNameOfVariant(
  coverage: VariantCoverage,
  key: string,
  collisions: string[],
): string {
  const label = nameOfVariant(coverage, key)
  return collisions.includes(key) ? `${label}(${key.slice(-6)})` : label
}

function toDraft(detail: ImageSetDetail | undefined): Draft[] {
  if (!detail) return []
  return detail.items.map((i) => ({
    key: rowKey(i.media_asset_id, i.variant_id),
    media_asset_id: i.media_asset_id,
    role: i.role,
    sort_order: i.sort_order,
    variant_id: i.variant_id,
    is_primary: i.is_primary,
    enabled: i.enabled,
    // A-19:这里和 `payload()` 都是逐字段列举,而两处都漏了这一个。
    // 界面上没有编辑它的入口,于是"漏掉"看起来毫无代价 —— 实际是:
    // 保存 = 派生新版 = 整批重建 items,后端按 `item.get(...)` 取值,
    // 取不到就落 NULL。**运营点一次保存,库里已有的派生用途就被清空**,
    // 而界面上没有任何东西会变,下一次导出才会用错图。
    //
    // 加编辑入口是另一件事(与 A-26 的行级变体选择器同类);
    // 在那之前,至少要**原样带回去**。
    derivative_purpose: i.derivative_purpose,
    // §6.5 的两列(2026-08-11 评审)。它们和 `derivative_purpose` 不同:
    // **批准闸真的会读它们**,所以除了带回去,下面每一行还有编辑入口。
    // 漏掉时的症状见 `api/imageSets.ts` 里 `ImageSetItemInput` 那一段
    angle: i.angle,
    shared_opt_in: i.shared_opt_in,
  }))
}

export default function ImageSetTab({
  productId,
  flowProductId = productId,
  spu,
  imageSetId,
  step,
}: {
  productId: string
  /** 跨色向导的缓存仍以入口商品为键。 */
  flowProductId?: string
  spu: string
  imageSetId: string | null
  step: FlowStepResult | undefined
}) {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft[] | null>(null)
  /** 本地草稿是基于服务端哪一版开始改的。没有本地改动时是 null(A-29) */
  const [draftBase, setDraftBase] = useState<number | null>(null)
  // A-25:拖拽状态也从 key 换成下标。key 在"同素材绑两个变体"时会撞,
  // 撞了之后拖 A 行会把 B 行拖走
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)

  const sets = useQuery({
    queryKey: ['image-sets', spu],
    queryFn: () => imageSetsApi.list(spu),
    enabled: Boolean(spu),
  })

  /** 详情里才有 items 与校验结果。没有当前集时不请求 */
  const current = useQuery({
    queryKey: ['image-set', imageSetId],
    queryFn: () => imageSetsApi.get(imageSetId as string),
    enabled: Boolean(imageSetId),
  })

  const assets = useQuery({
    queryKey: ['workbench-media', productId],
    queryFn: () => mediaApi.list({ product_id: productId, page_size: MEDIA_PAGE_SIZE }),
    enabled: Boolean(productId),
  })

  /**
   * 本地草稿的唯一入口。**所有编辑都走它,不要直接 `setDraft`。**
   *
   * 除了存改动,它还记住"这次改动是基于服务端哪一版开始的" ——
   * A-29 的横幅要靠这个数才知道该不该出现。
   */
  const applyEdit = (next: Draft[] | null) => {
    if (next === null) {
      setDraft(null)
      setDraftBase(null)
      return
    }
    if (draft === null) setDraftBase(current.data?.version ?? null)
    setDraft(next)
  }

  /*
   * A-29:后台刷新**不再静默清空**未保存的编排。
   *
   * 原来这个 effect 的依赖里有 `current.data?.version`:别人在这期间
   * 派生了新版(或者自己在另一个标签页保存了一次),一次后台 refetch
   * 就把 `draft` 置回 null —— 运营刚拖好的十几行,**没有任何提示地**
   * 变回服务端那一版。`UnsavedGuard` 挡的是离开页面,挡不住这条。
   *
   * 现在只有"换了一个图片集"才清:那时本地草稿指向的是另一件商品的
   * 编排,留着才是错的。版本变化交给下面的横幅,由人决定丢谁。
   */
  useEffect(() => {
    setDraft(null)
    setDraftBase(null)
  }, [imageSetId])

  const items = draft ?? toDraft(current.data)
  const dirty = draft !== null

  const violations = current.data?.violations ?? []
  // 读不到详情时是 null,不是空对象 —— 空对象会渲染成"0 个变体",
  // 而那是一个看起来像事实的猜测(同下面 detailBroken 那一段的取舍)。
  //
  // A-26 把它提到了本地编辑那一段之前:行级变体选择器的选项来自
  // `coverage.required`,拿不到它时那个下拉必须**禁用**而不是显示空列表 ——
  // 空列表看起来像"这个 SPU 没有颜色变体",而那是猜的
  const coverage = current.data?.variant_coverage ?? null
  // 后端 `drift` 的默认值是 `{}`,键在运行期可能不存在。在这里收敛一次,
  // 下面就不用每处都写 `?? []`(漏一处就是一次白屏)
  const labelCollisions = coverage?.drift.label_collisions ?? []
  /** A-28:同一个 key 下两个颜色。目录名会变成"看哪一行排在前面" */
  const keyLabelConflicts = coverage?.drift.key_label_conflicts ?? []
  /** 兼容旧后端:0046 后该字段恒为空,但混合版本响应仍可展示。 */
  const identityShadowed = coverage?.drift.identity_shadowed ?? []

  const assetById = useMemo(() => {
    const map = new Map<string, MediaAsset>()
    for (const a of assets.data?.items ?? []) map.set(a.id, a)
    return map
  }, [assets.data])

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['image-sets', spu] })
    queryClient.invalidateQueries({ queryKey: ['image-set', imageSetId] })
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', flowProductId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }

  /**
   * 写请求失败的统一收尾(FE-GLOBAL-01)。
   *
   * 保存会派生新版本、批准会改变当前在架的是哪一版 —— 两者超时后都不能
   * 说"失败了,请重试":重试可能派生出第三个版本,或者批准一版已经被批准过的集。
   * 结果未知时顺手刷新,版本历史会直接给出答案。
   */
  const onWriteError = (err: unknown) => {
    message.error(readWriteError(err))
    if (isResultUnknown(err)) refresh()
  }

  const payload = (list: Draft[]): ImageSetItemInput[] =>
    list.map((item, index) => ({
      media_asset_id: item.media_asset_id,
      role: item.role,
      sort_order: index,
      variant_id: item.variant_id ?? null,
      is_primary: Boolean(item.is_primary),
      enabled: item.enabled !== false,
      // A-19:见 `toDraft`。这两个函数的字段集必须和 `ImageSetItemInput`
      // 相等 —— `tests/pure/test_a44_batch3_fixes.py` 按类型定义逐字段核对,
      // 下一次给入参加字段时会红在这里,而不是等到导出用错图
      derivative_purpose: item.derivative_purpose ?? null,
      angle: item.angle ?? null,
      shared_opt_in: Boolean(item.shared_opt_in),
    }))

  const save = useMutation({
    mutationFn: async (list: Draft[]) => {
      const body = payload(list)
      if (!imageSetId) return imageSetsApi.create({ spu, items: body })
      // 接口没有「原地改 items」这条路径,版本是不可变的 —— 所以每次保存都是
      // 派生一个新版,草稿版也一样。代价是编排改三次会留下 v1/v2/v3,
      // 收益是任何一版都能被回滚和被追溯到,这是 M5 刻意的取舍。
      return imageSetsApi.derive(imageSetId, body)
    },
    onSuccess: (detail) => {
      message.success(`已保存 v${detail.version}(${detail.items.length} 张)`)
      applyEdit(null)
      queryClient.setQueryData(['image-set', detail.id], detail)
      refresh()
    },
    onError: onWriteError,
  })

  const approve = useMutation({
    mutationFn: (id: string) => imageSetsApi.approve(id),
    onSuccess: (detail) => {
      message.success(`v${detail.version} 已批准`)
      refresh()
    },
    onError: onWriteError,
  })

  const rollback = useMutation({
    mutationFn: (id: string) => imageSetsApi.rollback(id),
    onSuccess: (detail) => {
      message.success(`已回滚到 v${detail.version}`)
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * FE-IMAGESET-06:保存 / 批准 / 回滚三个动作互斥。
   *
   * 它们改的是同一个 SPU 的同一条版本链,而且顺序敏感 ——
   * "保存"派生新版、"批准"把某一版置为当前、"回滚"把旧版重新置为当前。
   * 并发发出的话,谁先到后端决定了最终是哪一版在架上,而界面上三个按钮
   * 各转各的圈,没有任何地方能看出发生了什么。
   *
   * 后端有 advisory lock,不会写坏数据 —— 但"不会写坏"和"结果是运营想要的那个"
   * 是两件事。这道锁挡的是后者。
   */
  const busy = save.isPending || approve.isPending || rollback.isPending

  // ---------------------------------------------------------- 本地编辑
  //
  // A-25:所有行操作一律**按下标**定位,不按 key 过滤。
  //
  // 按 key 过滤在"同一素材绑两个变体"这种合法数据上会一次命中两行 ——
  // 删一行删掉两行,而两行长得一模一样。下标是数组自己的身份,不会撞;
  // key 只用来当 React 的 `key`,不参与任何一次筛选。

  const move = (from: number, to: number) => {
    if (to < 0 || to >= items.length) return
    const next = [...items]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    applyEdit(next)
  }

  /** 这一对 (素材, 变体) 是不是已经在集里了 —— 后端唯一索引的前端版 */
  const taken = (mediaAssetId: string, variantId: string | null, exceptIndex = -1) =>
    items.some(
      (i, index) =>
        index !== exceptIndex &&
        i.media_asset_id === mediaAssetId &&
        (i.variant_id ?? '') === (variantId ?? ''),
    )

  const add = (asset: MediaAsset) => {
    // A-26 之后这里判的是**(素材, 变体) 这一对**,不再是"这张素材用过没有":
    // 同一张图绑到两个颜色上是合法的,拦掉它等于把闭环又堵死一半
    if (taken(asset.id, null)) {
      message.info('这张素材的通用图已经在集里了。要绑到某个颜色,用那一行的「再绑一个颜色」')
      return
    }
    applyEdit([
      ...items,
      {
        key: rowKey(asset.id, null),
        media_asset_id: asset.id,
        role: asset.role ?? 'OTHER',
        sort_order: items.length,
        // 默认仍是通用图。默认绑到某个颜色的话,单色 SPU 会平白多出一个
        // 谁也没要求过的标签,而那个标签会进 `unknown_tags`
        variant_id: null,
        // 第一张进来的图默认设为主图:一个集必须有主图,而忘设主图是最常见的校验失败
        is_primary: items.length === 0,
        enabled: true,
        // 两个默认值都和库里的列默认一致(§6.5)。**刻意不默认勾「通用」**:
        // 那一列的语义是"有人明确说过这张图可以混入各颜色",默认勾上等于
        // 让那句话变成一句没人说过的话
        angle: null,
        shared_opt_in: false,
      },
    ])
  }

  const remove = (index: number) => applyEdit(items.filter((_, i) => i !== index))

  const setPrimary = (index: number) =>
    applyEdit(items.map((item, i) => ({ ...item, is_primary: i === index })))

  const patch = (index: number, changes: Partial<Draft>) =>
    applyEdit(
      items.map((item, i) => {
        if (i !== index) return item
        const next = { ...item, ...changes }
        return { ...next, key: rowKey(next.media_asset_id, next.variant_id) }
      }),
    )

  const setRole = (index: number, role: MediaRole) => patch(index, { role })

  /**
   * 这一行覆盖的拍摄角度(§6.5,2026-08-11 评审)。
   *
   * `MISSING_REQUIRED_ANGLE` 的判据是**这一列**,不是图的内容:后端
   * `coverage()` 拿 `item.angle` 和方案 `angles_json` 的键比集合。所以没有
   * 这个入口时,配了「正面 + 背面」的方案永远报"缺角度",而运营唯一能做的
   * 事(补图)一点用都没有 —— 补进来的图 `angle` 还是空的。
   *
   * 候选图由生成链路自动带角度(`GenerationCandidate.candidate_metadata`
   * 的 `target_angle`,由 `image_set_service` 在缺省时继承),
   * 所以这个下拉主要用于**人工上传的图与例外**,与 schema 里那句
   * 「人工只处理例外」一致。
   */
  const setAngle = (index: number, angle: ImageAngle | null) => patch(index, { angle })

  /**
   * 「这张通用图允许混入颜色附图」(§6.5)。
   *
   * 只对通用图(`variant_id === null`)有意义:绑了颜色的行本来就属于那个颜色,
   * 这一列不参与任何判定。**不勾的通用图会在批准时报
   * `UNMARKED_SHARED_IMAGE`**,而在这个开关出现之前那条阻断无处可解。
   */
  const setSharedOptIn = (index: number, shared: boolean) =>
    patch(index, { shared_opt_in: shared })

  /**
   * A-26:给某一行绑颜色变体。**这是 B-01 缺的那个入口。**
   *
   * 在这之前 `add()` 把 `variant_id` 硬编码成 null,界面上没有任何地方
   * 能改它 —— 于是覆盖横幅会告诉运营"这个 SPU 有 3 个颜色,这一版绑定了 0 个",
   * 而他无处可点。后端从 A42 起就支持了,前端一直没接。
   *
   * 传的是**稳定 key**(`coverage.required` 里那个),不是颜色名:
   * `resolve_ref` 的第一级就是 key 完全相等,而颜色名那一级在两个变体
   * 撞名时会返回 None。界面上显示名字,提交的是身份。
   */
  const setVariant = (index: number, variantId: string | null) => {
    if (taken(items[index].media_asset_id, variantId, index)) {
      // 后端会 422(`第 N 项重复`),但那要等到点保存。在这一刻说更省一轮
      message.warning('这张素材已经绑过这个颜色了 —— 同一张图的同一个颜色只能出现一次')
      return
    }
    patch(index, { variant_id: variantId })
  }

  /**
   * 把这一行再绑到另一个颜色上。
   *
   * "同一张素材绑两个变体"是合法数据(后端唯一键带 variant_id),
   * 但在 A-26 之前**前端造不出来**,只有经 API 才能。于是 A-25 那个
   * 重复 key 的 bug 在界面上永远复现不了,也就永远修不掉。
   *
   * 找第一个还没被这张素材占掉的颜色;都占满了就退回通用图;再没有就不给点。
   */
  const duplicateForVariant = (index: number) => {
    const source = items[index]
    const free = [...(coverage?.required ?? []), null].find(
      (candidate) => !taken(source.media_asset_id, candidate ?? null),
    )
    if (free === undefined) {
      message.info('这张素材已经绑遍了本 SPU 的所有颜色')
      return
    }
    const copy: Draft = {
      ...source,
      key: rowKey(source.media_asset_id, free),
      variant_id: free,
      // 复制出来的行不跟着当主图:一个集只能有一张主图,复制主图会让
      // 界面上出现两个高亮的"主图"按钮,而保存后只有一个生效
      is_primary: false,
    }
    const next = [...items]
    next.splice(index + 1, 0, copy)
    applyEdit(next)
  }
  /*
   * FE-IMAGESET-01~03(BLOCK-12):三条 query 失败时**不许退化成空数据**。
   *
   * 上一版三处都写成 `data ?? []`,后果按严重程度排:
   *
   *   current 失败   items 空、violations 空 -> `canApprove` 计算出 **true**。
   *                  于是接口读不到这一版内容的时候,"批准图片集"是亮的 ——
   *                  运营会批准一版他根本没看到的编排
   *   assets 失败    可选素材区显示"所有可用素材都已在集里",
   *                  运营据此认为素材已经齐了
   *   sets 失败      版本历史整块消失,看起来像"这个 SPU 只有一版"
   *
   * 所以这里把"读不到"和"没有"分成两个事实,并让读不到的那一支**阻断写动作**:
   * 拿不到当前状态时,任何基于当前状态的判断都是猜的。
   */
  /**
   * A-29:我在改的这一版,已经被别人换掉了。
   *
   * 出现的时机:后台 refetch(窗口重新聚焦、`refresh()`、轮询)拿回了一个
   * 比我开始改时更新的版本。改之前这一刻会静默丢掉我的改动;现在改动留着,
   * 由这个横幅告诉我发生了什么,丢谁由我决定。
   */
  const supersededBy =
    dirty && draftBase !== null && current.data && current.data.version !== draftBase
      ? current.data.version
      : null

  const detailBroken = Boolean(imageSetId) && current.isError
  const assetsBroken = assets.isError

  /**
   * 这份素材清单**没拉全**(第 `MEDIA_PAGE_SIZE + 1` 张之后的查不到)。
   *
   * 下面那个"不在本 SKU"的格子原来枚举了三种成因,而**第四种一直没写**:
   * 这张图就在这个 SKU 下,只是排在第 201 位。一个素材量大的 SPU 会让运营
   * 对着一张真实存在的图读到「也可能已被删除,到素材库查证后再决定是否移除」
   * —— 那句话的下一步是删掉它。
   *
   * 分不开的时候不许说死(同格子那段注释的原话)。所以拿到这个事实就
   * 加一档,而不是继续用一句涵盖不了它的话。
   */
  const assetsTruncated = (assets.data?.total ?? 0) > (assets.data?.items.length ?? 0)

  /*
   * 阻断范围要精确到"哪个读失败挡哪个动作",不能一刀切:
   *
   *   detailBroken -> 挡批准。violations 来自详情,读不到时它是空的,
   *                   而批准的全部依据就是"没有校验问题"
   *   assetsBroken -> **不挡**。编排本身在详情里,素材清单只影响缩略图和
   *                   可选区。挡了的话,一次后台刷新失败就会把运营已经拖好的
   *                   顺序锁死,而那份编排本来是可以安全保存的
   *
   * 一刀切的代价是真实的:素材接口抖一下,运营刚拖完的十几张图就存不下去。
   */
  const canApprove =
    Boolean(imageSetId) &&
    !dirty &&
    !detailBroken &&
    !busy &&
    Boolean(current.data) &&
    violations.length === 0 &&
    current.data?.status !== 'APPROVED'

  const selectable = (assets.data?.items ?? []).filter((a) => !items.some((i) => i.media_asset_id === a.id))

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <UnsavedGuard dirty={dirty} what="图片集" />

      {/* 读不到就说读不到。放在最上面 —— 下面每一块内容都因此不可信 */}
      {detailBroken && (
        <ErrorNotice
          title="读不到当前图片集"
          error={current.error}
          onRetry={() => current.refetch()}
          retrying={current.isFetching}
        />
      )}
      {assetsBroken && (
        <ErrorNotice
          title="读不到这件商品的素材"
          error={assets.error}
          onRetry={() => assets.refetch()}
          retrying={assets.isFetching}
        />
      )}
      {sets.isError && (
        <ErrorNotice
          type="warning"
          title="读不到版本历史"
          error={sets.error}
          onRetry={() => sets.refetch()}
          retrying={sets.isFetching}
        />
      )}
      {detailBroken && (
        <Alert
          type="warning"
          showIcon
          message="读不到当前图片集,批准已暂时禁用"
          description="校验结果来自这一条接口。读不到时,「没有校验问题」是猜的 —— 而批准的依据正是它。先重试上面的读取。"
        />
      )}

      {supersededBy !== null && (
        <Alert
          type="warning"
          showIcon
          message={`你在改的是 v${draftBase},服务端现在已经是 v${supersededBy}`}
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <span>
                你的改动<b>没有丢</b>,还在下面。但它是基于旧的那一版改的 ——
                保存会用当前编排另存一个新版本,别人在 v{supersededBy} 里做的改动
                不会自动合并进来。
              </span>
              <Space>
                <Button size="small" danger onClick={() => applyEdit(null)}>
                  放弃我的改动,载入 v{supersededBy}
                </Button>
                <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
                  想留着就直接保存;两边都想要的话,先记下自己改了什么再载入新版。
                </span>
              </Space>
            </Space>
          }
        />
      )}

      {step && step.issues.length > 0 && (
        <Card size="small" title="这一步的问题">
          <IssueList issues={step.issues} />
        </Card>
      )}

      <RejectNotice
        target="image_set"
        reason={current.data?.reject_reason ?? null}
        note={current.data?.reject_note ?? null}
        at={current.data?.rejected_at ?? null}
      />

      {/* FE-213:阻断与提醒分级展示。图片集的校验结果一律阻断批准 */}
      {violations.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`${violations.length} 条校验问题,修完才能批准`}
          description={
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {violations.map((v, index) => (
                <li key={`${v.code}-${index}`}>
                  {v.message}
                  <span className="mono" style={{ color: brandVars.textMuted, marginLeft: 6 }}>
                    {v.code}
                  </span>
                </li>
              ))}
            </ul>
          }
        />
      )}

      {/*
        A44:变体覆盖诊断第一次被显示出来。

        后端从 A42 起就在算它(`variant_coverage`),而前端一个字都没读 ——
        一个算好了没人看的诊断和没有这个诊断是同一件事,只是前者让人以为
        自己有。

        **这一块只做展示,但「不阻断批准」那半句已经过期**(2026-08-09 评审订正)。
        原文的理由是「今天绝大多数 SPU 的图全是通用图,改成阻断等于停产」,
        而 A-26 补上行级颜色选择器、`coverage()` 删掉「有通用图就算覆盖」的
        放行之后,后端 `approve()` 真的会因为缺色图抛 409。所以这里显示的
        缺口**就是批准时会被拦下来的那一份** —— 它是提前预告,不是仅供参考。

        显示的一律是 `labels[key]` 而不是 key 本身。key 是变体身份,取值是
        分配那一刻的颜色名 —— 改过名之后直接显示 key,运营会看到自己刚改掉
        的旧名字,然后再改一次。
      */}
      {coverage && coverage.required.length > 1 && (
        <Alert
          type={labelCollisions.length > 0 || identityShadowed.length > 0 ? 'error' : 'info'}
          showIcon
          message={`这个 SPU 有 ${coverage.required.length} 个颜色变体,这一版绑定了 ${coverage.tagged.length} 个`}
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space size={4} wrap>
                {coverage.required.map((key) => (
                  coverage.tagged.includes(key) ? (
                    <BrandTag key={key} tone="success">
                      {nameOfVariant(coverage, key)}
                    </BrandTag>
                  ) : (
                    <Tag key={key}>{nameOfVariant(coverage, key)}</Tag>
                  )
                ))}
              </Space>
              {coverage.variant_binding_missing && (
                <span style={{ color: brandVars.textMuted }}>
                  这一版全是通用图。平台按颜色取图时给不出「红色的主图是哪张」——
                  批准不会拦(会先问一次),但上架之后每个颜色拿到的会是同一张。
                  <b>要绑就在下面每一行的颜色下拉里选</b>;同一张图要给多个颜色用,
                  点那一行的复制按钮。
                </span>
              )}
              {labelCollisions.length > 0 && (
                <span>
                  <b>有两个变体现在同名</b>(
                  {labelCollisions
                    .map((k) => distinctNameOfVariant(coverage, k, labelCollisions))
                    .join('、')}
                  )。绑图时分不出该给哪一个,提交也会被拒 —— 先把颜色名改开。
                </span>
              )}
              {keyLabelConflicts.length > 0 && (
                <span>
                  <b>这些变体下挂着两个不同的颜色</b>(
                  {keyLabelConflicts.map((k) => nameOfVariant(coverage, k)).join('、')}
                  )。变体目录里显示哪个名字取决于商品行的顺序 ——
                  多半是两行本该是两个变体却共用了 key,或者其中一行的颜色被单独改过。
                </span>
              )}
              {identityShadowed.length > 0 && (
                <span>
                  <b>这些 SKU 同时带着颜色变体外键和旧 variant_key,外键已经接管身份</b>(
                  {identityShadowed.join('、')}
                  )。它们已确认的颜色属性和已绑的图片标签仍指着旧 key ——
                  说明有人回填了外键却没有同步改写属性与标签。先停下回填,
                  按 DECISIONS §3.23 把三样东西放进同一个动作里做。
                </span>
              )}
              {coverage.unknown_tags.length > 0 && (
                <span>
                  这些标签在本 SPU 找不到对应变体:{coverage.unknown_tags.join('、')}。
                  通常是打完标签之后那一行商品被删了或换了 SPU。
                </span>
              )}
            </Space>
          }
        />
      )}

      <Card
        size="small"
        title={
          <Space size={8}>
            <span>图片集编排</span>
            {current.data && (
              <>
                <Tag>v{current.data.version}</Tag>
                <Tag color={IMAGE_SET_STATUS_LABEL[current.data.status]?.color}>
                  {IMAGE_SET_STATUS_LABEL[current.data.status]?.text ?? current.data.status}
                </Tag>
                {dirty && <Tag color="warning">有未保存的改动</Tag>}
              </>
            )}
          </Space>
        }
        extra={
          <Space>
            {dirty && (
              <Button size="small" onClick={() => applyEdit(null)}>
                放弃改动
              </Button>
            )}
            <Button
              size="small"
              type={dirty ? 'primary' : 'default'}
              disabled={!dirty || !items.length || busy}
              loading={save.isPending}
              onClick={() => {
                if (current.data?.status === 'APPROVED') {
                  modal.confirm({
                    title: '这一版已批准,保存会另存成一个新版本',
                    content: '已批准的图片集不原地修改 —— 否则「平台驳回的是哪一版」就没法回答。新版从待批准开始。',
                    okText: '另存新版本',
                    onOk: () => save.mutate(items),
                  })
                } else {
                  save.mutate(items)
                }
              }}
            >
              保存编排
            </Button>
            <Tooltip
              title={
                /*
                 * A-30:这条链原来漏了"还在加载"和"根本没有图片集"两支,
                 * 于是按钮是**禁用**的、提示却是"提交批准" —— 一个可点的
                 * 说法配一个点不动的按钮。`canApprove` 里有六个条件,而这里
                 * 只解释了其中四个;缺的那两个恰恰是首屏最常见的状态。
                 *
                 * 顺序要和 `canApprove` 的求值顺序对齐:先说最外层的原因。
                 */
                !imageSetId
                  ? '这个 SPU 还没有图片集,先保存一版编排'
                  : detailBroken
                    ? '当前图片集读取失败,拿不到校验结果时不能批准'
                    : current.isLoading
                      ? '正在读取当前图片集'
                      : busy
                        ? '有一个操作正在进行,等它结束'
                        : current.data?.status === 'APPROVED'
                          ? '这一版已经批准了'
                          : dirty
                            ? '先保存编排再批准'
                            : violations.length
                              ? '有校验问题,不能批准'
                              : '提交批准'
              }
            >
              <Button
                size="small"
                type="primary"
                disabled={!canApprove}
                loading={approve.isPending}
                onClick={() => {
                  if (!imageSetId) return
                  /*
                   * B-01 的第三块:批准之前把"每个颜色拿到的会是同一张"说清楚。
                   *
                   * **刻意不做成硬阻断。** 今天在架的集全是通用图,改成阻断
                   * 等于让每一个多色 SPU 立刻无法批准 —— 那不是修复是停产
                   * (`variant_coverage` 的 docstring 里写的就是这条)。
                   *
                   * 但 A-26 之后"绑不了"这个理由已经不成立了:入口就在上面
                   * 每一行里。所以这里从"默默放行"改成"**知情放行**" ——
                   * 说清代价、让人自己决定,并且这一次点击本身进审计。
                   *
                   * 要不要升级成硬阻断是**业务规则决定**,不是代码决定:
                   * 它取决于存量集要不要先补绑一轮。留给 B-01 的业务侧。
                   */
                  if (
                    coverage &&
                    coverage.required.length > 1 &&
                    coverage.variant_binding_missing
                  ) {
                    modal.confirm({
                      title: `这一版全是通用图,${coverage.required.length} 个颜色一张都没绑`,
                      content:
                        '平台按颜色取图时给不出「这个颜色的主图是哪张」,上架之后每个颜色拿到的会是同一张。要绑的话,在每一行的颜色下拉里选,或者用「再绑一个颜色」把同一张图绑到多个颜色。',
                      okText: '仍然批准',
                      cancelText: '回去绑图',
                      onOk: () => approve.mutate(imageSetId),
                    })
                    return
                  }
                  approve.mutate(imageSetId)
                }}
              >
                批准图片集
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {current.isLoading || assets.isLoading ? (
          <Skeleton active />
        ) : items.length ? (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {items.map((item, index) => {
              const asset = assetById.get(item.media_asset_id)
              return (
                <div
                  key={item.key}
                  className="imageset-row"
                  draggable
                  data-dragging={dragIndex === index}
                  data-dropbefore={overIndex === index && dragIndex !== index}
                  onDragStart={() => setDragIndex(index)}
                  onDragEnd={() => {
                    setDragIndex(null)
                    setOverIndex(null)
                  }}
                  onDragOver={(e) => {
                    e.preventDefault()
                    // A45-#24:`dragover` 每几毫秒触发一次,无条件 setState 会让
                    // 整个列表在拖动期间持续重渲染(图片越多越明显)。只有落点
                    // 真的换行时才更新 —— React 对相同值的 setState 不保证跳过重渲染
                    if (overIndex !== index) setOverIndex(index)
                  }}
                  onDrop={(e) => {
                    e.preventDefault()
                    if (dragIndex === null || dragIndex === index) return
                    move(dragIndex, index)
                    setDragIndex(null)
                    setOverIndex(null)
                  }}
                >
                  <span className="mono" style={{ color: brandVars.textMuted, width: 18 }}>
                    {index + 1}
                  </span>
                  {/* 走查 P0-3:原本是 56×56 的裸 <img>。拖拽排序的依据正是画面内容
                      (哪张当主图、正面在前还是细节在前),56px 下看不清在排什么,
                      而且点不开。现在提到 96px 并接上灯箱 */}
                  {asset ? (
                    <Image
                      src={asset.url}
                      alt={`第 ${index + 1} 张 · ${MEDIA_ROLE_LABEL[item.role] ?? item.role}`}
                      preview={{ mask: '看大图' }}
                      // A45-#28:私有图的签名 URL 有 TTL,而这一页不轮询 ——
                      // 挂机久了整页图一起 403,现象酷似"图坏了"。加载失败时
                      // 重新拉一次素材(那会带回新签的地址)。
                      //
                      // `refetch` 而不是 `invalidate`:这一次失败要的是立刻重签,
                      // 不是把缓存标脏等下一个订阅者。失败仍然失败的话,
                      // 下面那个"素材缺失"格子会照常出现,不会陷进重试循环
                      onError={() => {
                        if (!assets.isFetching) void assets.refetch()
                      }}
                    />
                  ) : (
                    /*
                     * 这个格子原来一律写"素材缺失",而它其实有**四种**成因,
                     * 只有一种是坏事:
                     *
                     *   素材接口失败       什么都不知道
                     *   清单没拉全         只取前 MEDIA_PAGE_SIZE 张,这一张排在后面。
                     *                      它就在本 SKU 下,一点问题都没有
                     *   属于同 SPU 的兄弟 SKU   图片集是 SPU 级的,而这里的素材只按
                     *                      product_id 拉 —— 兄弟 SKU 加进来的图
                     *                      本来就不在这份清单里,并**不是**缺失
                     *   真的被删了         这才是要报警的那一种
                     *
                     * 分不开的时候不许说死。说死的代价是运营把一张好图删掉 ——
                     * 而"清单没拉全"这一档原来正是被那句说死的话涵盖着的
                     * (前端评审 P2-6):素材量大的 SPU 上,一张好端端的图会被
                     * 指引去"查证后再决定是否移除"。
                     */
                    <Tooltip
                      title={
                        assetsBroken
                          ? '素材接口读取失败,无法确认这一张是否还在'
                          : assetsTruncated
                            ? `本 SKU 的素材超过 ${MEDIA_PAGE_SIZE} 张,这一页只取了前 ${MEDIA_PAGE_SIZE} 张 —— 这张图很可能就在本 SKU 下,只是没被载入。先去素材库确认,不要直接移除`
                            : '这张图不在本 SKU 的素材清单里。图片集是 SPU 级的,它可能属于同 SPU 的其他 SKU;也可能已被删除。到素材库按 SPU 查证后再决定是否移除'
                      }
                    >
                      <div
                        style={{
                          width: imageTile.compact, height: imageTile.compact,
                          background: brandVars.dangerBg, borderRadius: 3,
                          display: 'grid', placeItems: 'center', textAlign: 'center',
                          color: brandVars.danger, fontSize: fontScale.meta, padding: 2,
                        }}
                      >
                        {assetsBroken ? '读不到' : assetsTruncated ? '未载入' : '不在本 SKU'}
                      </div>
                    </Tooltip>
                  )}
                  <Select<MediaRole>
                    size="small"
                    style={{ width: 120 }}
                    value={item.role}
                    onChange={(role) => setRole(index, role)}
                    options={Object.entries(MEDIA_ROLE_LABEL).map(([value, label]) => ({
                      value: value as MediaRole,
                      label,
                    }))}
                  />
                  {/*
                    A-26 / B-01:行级变体绑定入口。**这一格就是那个缺失的闭环。**

                    在这之前 `variant_id` 恒为 null,而上面的覆盖横幅会告诉运营
                    「这个 SPU 有 3 个颜色,这一版绑定了 0 个」——一个说明问题
                    却无处可点的提示,比没有提示更让人不信任界面。

                    值是稳定 key,显示是当前颜色名。两个变体现在同名时(改名撞车)
                    在名字后面缀上 key 的尾巴 —— 否则下拉里会出现两个「红色」,
                    而选错的后果要到平台侧才暴露。
                  */}
                  <Tooltip
                    title={
                      !coverage
                        ? '读不到变体清单,暂时不能改绑定'
                        : coverage.required.length === 0
                          ? '这个 SPU 没有颜色变体'
                          : '通用图对所有颜色生效;绑到某个颜色后,平台按颜色取图时会优先用它'
                    }
                  >
                    <Select<string>
                      size="small"
                      style={{ width: 150 }}
                      disabled={!coverage || coverage.required.length === 0}
                      value={item.variant_id ?? ''}
                      onChange={(value) => setVariant(index, value || null)}
                      options={[
                        { value: '', label: '通用图(所有颜色)' },
                        ...(coverage?.required ?? []).map((key) => ({
                          value: key,
                          label: distinctNameOfVariant(
                            coverage as VariantCoverage,
                            key,
                            labelCollisions,
                          ),
                        })),
                      ]}
                    />
                  </Tooltip>
                  {/*
                    2026-08-11 评审:角度入口。**这一格就是 `MISSING_REQUIRED_ANGLE`
                    唯一的解法。** 判据是这一列而不是图的内容,所以在它出现之前,
                    配了「正面 + 背面」的方案会让运营看到一条他补图也解不掉的阻断。

                    选项表来自 `api/generationPlans.IMAGE_ANGLE_LABEL`,那份表与
                    后端 `core/enums.ImageAngle` 逐值对应 —— 不在这里另写一份。
                  */}
                  <Tooltip title="这张图覆盖的拍摄角度。生成出来的候选图会自动带上方案角度,这里用于人工上传的图与例外">
                    <Select<string>
                      size="small"
                      style={{ width: 128 }}
                      value={item.angle ?? ''}
                      onChange={(value) => setAngle(index, (value || null) as ImageAngle | null)}
                      options={[
                        { value: '', label: '角度未标' },
                        ...(Object.keys(IMAGE_ANGLE_LABEL) as ImageAngle[]).map((value) => ({
                          value,
                          label: IMAGE_ANGLE_LABEL[value],
                        })),
                      ]}
                    />
                  </Tooltip>
                  <Tooltip title={item.is_primary ? '当前主图' : '设为主图'}>
                    <Button
                      size="small"
                      type={item.is_primary ? 'primary' : 'default'}
                      icon={item.is_primary ? <StarFilled /> : <StarOutlined />}
                      onClick={() => setPrimary(index)}
                    >
                      主图
                    </Button>
                  </Tooltip>
                  {/*
                    §6.5 的「通用图默认不混入颜色附图」。只对通用图有意义 ——
                    绑了颜色的行本来就属于那个颜色,后端 `_section_6_5` 直接
                    `continue` 掉。所以那些行禁用而不是隐藏:隐藏会让同一列在
                    不同行上时有时无,而运营会以为是渲染出错了。
                  */}
                  <Tooltip
                    title={
                      item.variant_id
                        ? '这一行已绑到某个颜色,不需要标「通用」'
                        : '勾上之后,这张通用图才会混入各颜色的附图位;不勾会在批准时被拦下(UNMARKED_SHARED_IMAGE)'
                    }
                  >
                    <Checkbox
                      disabled={Boolean(item.variant_id)}
                      checked={Boolean(item.shared_opt_in)}
                      onChange={(e) => setSharedOptIn(index, e.target.checked)}
                    >
                      通用
                    </Checkbox>
                  </Tooltip>
                  {asset?.status === 'QUARANTINED' && (
                    <Tag color="error">素材已隔离</Tag>
                  )}
                  <span style={{ flex: 1 }} />
                  {/* A69:这一排四个按钮只有图标。隔壁"复制到另一个颜色"早就包了
                      Tooltip,约定在,只是没铺满 —— 补齐它。顺序影响的是主图之外
                      的排布,而排布就是平台看到的顺序,所以这两个不是装饰按钮 */}
                  <Tooltip title="上移一位">
                    <Button
                      size="small"
                      aria-label="上移一位"
                      icon={<ArrowUpOutlined />}
                      disabled={index === 0}
                      onClick={() => move(index, index - 1)}
                    />
                  </Tooltip>
                  <Tooltip title="下移一位">
                    <Button
                      size="small"
                      aria-label="下移一位"
                      icon={<ArrowDownOutlined />}
                      disabled={index === items.length - 1}
                      onClick={() => move(index, index + 1)}
                    />
                  </Tooltip>
                  <Tooltip title="把这张图再绑到另一个颜色上(同一张素材绑多个颜色是允许的)">
                    <Button
                      size="small"
                      aria-label="把这张图再绑到另一个颜色上"
                      icon={<CopyOutlined />}
                      disabled={!coverage || coverage.required.length === 0}
                      onClick={() => duplicateForVariant(index)}
                    />
                  </Tooltip>
                  {/* 只从这一版编排里去掉,不删素材本身 —— 措辞要说清这件事:
                      运营看到一个红色垃圾桶,默认会以为图没了 */}
                  <Tooltip title="从这一版编排里移除（不删除素材）">
                    <Button
                      size="small"
                      danger
                      aria-label="从这一版编排里移除（不删除素材）"
                      icon={<DeleteOutlined />}
                      onClick={() => remove(index)}
                    />
                  </Tooltip>
                </div>
              )
            })}
          </Space>
        ) : (
          <Empty description="还没有编排。从下面的素材里挑图,第一张会自动设为主图。" />
        )}
      </Card>

      <Card size="small" title={`可选素材 · ${selectable.length}`}>
        {selectable.length ? (
          <div className="asset-grid">
            {selectable.map((asset) => {
              const blocked = asset.status !== 'READY'
              return (
                <figure key={asset.id} className="asset-tile" style={{ margin: 0, opacity: blocked ? 0.55 : 1 }}>
                  <Image
                    src={asset.url}
                    alt={`可选素材 · ${asset.role ? MEDIA_ROLE_LABEL[asset.role] : '未定角色'}`}
                    preview={{ mask: '看大图' }}
                  />
                  <figcaption>
                    <Space size={4} wrap style={{ marginBottom: 4 }}>
                      <Tag>{asset.role ? MEDIA_ROLE_LABEL[asset.role] : '未定角色'}</Tag>
                      {blocked && (
                        <Tag color="error">{MEDIA_STATUS_LABEL[asset.status]?.text ?? asset.status}</Tag>
                      )}
                    </Space>
                    <div>
                      {asset.width}×{asset.height}
                    </div>
                    {blocked ? (
                      <Tooltip title={asset.quarantine_reason ?? '素材不可用,先在素材标签页处理'}>
                        <span style={{ color: brandVars.danger, fontSize: fontScale.meta }}>不可选</span>
                      </Tooltip>
                    ) : (
                      <Button size="small" type="link" style={{ padding: 0 }} icon={<PlusOutlined />} onClick={() => add(asset)}>
                        加入
                      </Button>
                    )}
                  </figcaption>
                </figure>
              )
            })}
          </div>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              assetsBroken
                ? '素材读取失败,这里不代表没有可选素材 —— 先重试上面的读取'
                : '所有可用素材都已在集里。'
            }
          />
        )}
      </Card>

      {/* 读失败时上面已经有一条明确的错误横幅,这里只在"真的读到了"时渲染 */}
      {!sets.isError && (sets.data?.length ?? 0) > 1 && (
        <Card size="small" title="版本历史">
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            {sets.data?.map((s) => (
              <Space key={s.id} size={8}>
                <Tag>v{s.version}</Tag>
                <Tag color={IMAGE_SET_STATUS_LABEL[s.status]?.color}>
                  {IMAGE_SET_STATUS_LABEL[s.status]?.text ?? s.status}
                </Tag>
                <span style={{ color: brandVars.slate }}>
                  {s.approved_by
                    ? `${s.approved_by} 批准于 ${formatDateTime(s.approved_at)}`
                    : formatDateTime(s.created_at)}
                </span>
                {/*
                  * FE-IMAGESET-05:按钮条件原来是 `status !== 'APPROVED'`,
                  * 于是 DRAFT / PENDING_REVIEW / REJECTED 三种状态上都挂着一个
                  * "回滚到这一版" —— 而后端只接受 ARCHIVED 与 APPROVED,
                  * 点下去稳定 409。一个永远失败的按钮比没有按钮更糟:
                  * 它让人以为这条路走得通,只是今天坏了。
                  *
                  * 已批准的那一版不显示 —— 回滚到当前版本没有意义。
                  */}
                {s.status === 'ARCHIVED' && (
                  <Button
                    size="small"
                    type="link"
                    style={{ padding: 0 }}
                    disabled={busy}
                    loading={rollback.isPending && rollback.variables === s.id}
                    onClick={() =>
                      modal.confirm({
                        title: `回滚到 v${s.version}?`,
                        content:
                          '回滚会把这一版重新设为当前版本,并在历史里留下回滚记录。' +
                          '当前已批准的那一版会被归档。',
                        onOk: async () => {
                          try {
                            await rollback.mutateAsync(s.id)
                          } catch {
                            /* 错误已由 onWriteError 提示 */
                          }
                        },
                      })
                    }
                  >
                    回滚到这一版
                  </Button>
                )}
              </Space>
            ))}
          </Space>
        </Card>
      )}
    </Space>
  )
}
