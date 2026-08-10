import type { MediaAsset, MediaRole } from '../../api/media'
import { colorVariantLabel, type ColorVariant } from '../../api/spus'

/** 与后端 MediaRole 同集合。缺一个角色这里就少一个选项,不影响判定,只影响能不能改 */
export const MEDIA_ROLE_LABEL: Record<MediaRole, string> = {
  PRODUCT_FRONT: '正面图',
  PRODUCT_BACK: '背面图',
  MODEL_FRONT: '模特正面',
  MODEL_BACK: '模特背面',
  DETAIL: '细节图',
  SIZE_CHART: '尺码表',
  FLAT_LAY: '平铺图',
  PACKAGING: '包装图',
  OTHER: '其它',
}

/** 「通用图」这一组的键。空串不做键 —— 它和“没选”在 Select 里长得一样 */
export const GENERIC = '__generic__'

/**
 * 素材按颜色分组。
 *
 * `color_variant_id` 是已确认归属，`variant_hint` 只是模型猜测，不能拿来分组。
 * 键取素材实际归属与 SPU 声明颜色的并集，避免已下架颜色上的存量图凭空消失。
 */
export function groupByColour(
  assets: MediaAsset[],
  variants: ColorVariant[],
): { key: string; label: string; assets: MediaAsset[] }[] {
  const label = new Map(variants.map((v) => [v.id, colorVariantLabel(v)]))
  const buckets = new Map<string, MediaAsset[]>()
  for (const v of variants) buckets.set(v.id, [])
  for (const a of assets) {
    const key = a.color_variant_id ?? GENERIC
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key)!.push(a)
  }
  const colours = [...buckets.entries()]
    .filter(([key]) => key !== GENERIC)
    .map(([key, rows]) => ({ key, label: label.get(key) ?? key, assets: rows }))
  const generic = buckets.get(GENERIC) ?? []
  return generic.length || colours.length
    ? [...colours, { key: GENERIC, label: '通用图(不属于某个颜色)', assets: generic }]
    : []
}
