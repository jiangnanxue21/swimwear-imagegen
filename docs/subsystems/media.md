# media · 素材域

**目录**:`backend/app/media/`

## 四条来源,一个入口

```
人工上传    asset_service.upload_asset()          -> shadow_from_product_asset()
外链导入 / 供应商推送                              -> ingest()
AI 生成     generation_tasks._persist_candidates  -> shadow_from_candidate()
成品图      output_service.build_outputs()        -> shadow_from_output_asset()
```

不管从哪儿来,最后都落成 `media_assets` 的一行 —— 于是「这张图哪来的、归谁、
能不能用作证据」只有一处答案。

## 三条不能破的约定

**落盘路径由 sha256 推导**(`ab/cd/<hash>.jpg`)。用户提供的文件名只用于展示,
不参与路径拼接 —— 路径穿越在这里被结构性地消掉,而不是靠一层过滤。

**原始上传文件永不覆盖。** 同一个哈希再次上传时命中去重并如实告诉调用方;
成品图重算也不删旧图(网站可能还缓存着旧 URL,直接删会造成一批 404),
而是把旧记录 `enabled=False` 并把新记录的 `generation` 加一。

**输出图也走内容寻址。** 和素材同一套 sha256 分片路径,因此不同商品若渲染出完全
相同的缩略图(纯色背景很容易撞),存储层自动去重,数据库仍是两条记录。

## 角色必须是人工确认口径

门禁读取的 `role` 必须满足 `role_source ∈ {HUMAN, CONFIRMED}`。模型建议的角色只用于
预填与提示,**不满足**「至少一张正面图 / 平铺图」的完整度判定 —— 沿用「模型定的角色
不能直接用于主图位」那条既有规矩,并扩展到流程门禁。

## 证据分层

`EvidenceClass` 回答「这张图能不能用来支撑一条属性事实」。同一件商品的不同来源
(原始样品 vs AI 生成图)证据强度不同,冲突时按来源判优先级,
`provenance_conflict.py` 负责把冲突如实标出来而不是静默取其一。

跨颜色重复必须人工确认:去重键包含颜色维度,一张图在两个颜色下都出现是有意义的信号。

## 私有素材怎么给浏览器看

`<img>` 标签带不了自定义请求头,所以私有素材走 `/api/media-files/*` 的**签名代发**:
URL 自带签名与有效期,接口自己验签,而不是挂在通用口令闸上。
