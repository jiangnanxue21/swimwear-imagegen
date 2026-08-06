# 示例数据

| 文件 | 说明 |
| --- | --- |
| `products.csv` | 10 个示例泳装商品,可直接用于 `POST /api/products/import`。**这里写「泳装」是如实描述这批样例的品类**,不是系统边界 —— 系统是服装的,泳装是目前唯一已校准、且有渠道 spec 的品类(`docs/DECISIONS.md` §3.20) |
| `generate_images.py` | 生成占位素材图(每个 SKU 三张:正面 / 背面 / 细节) |
| `images/` | 生成结果,30 张 900×1200 JPEG |

```bash
python3 sample-data/generate_images.py    # 重新生成图片
make seed                                  # 导入商品与素材(幂等,可重复执行)
```

**这些图不是真实商品照片**,只是带图案的占位图,用来让上传、哈希去重、尺寸探测和详情页在没有真实素材时也能跑通。接入真实 Provider 前请替换。

`products.csv` 的 `notes` 列刻意标注了每个 SKU 的观察重点(条纹易扭曲、无肩带易被改写、交叉背带易变形等),阶段 4 做评分校准时可以直接拿来当测试样本。
