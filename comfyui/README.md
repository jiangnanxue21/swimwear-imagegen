# ComfyUI 接入说明

`app/providers/comfyui.py` 目前是**骨架**:能力声明、配置检查、连接测试、
`NotConfiguredError` 都可用,请求映射待你提供真实工作流后填写。

本文档说明**接入需要你做什么**,以及代码这边已经准备好了什么。
FASHN 那次接入证明了业务代码不用为 Provider 改动(见 `docs/PROVIDER-FASHN.md`),
ComfyUI 是对这个结论更强的一次检验 —— 它的形状(自建服务、工作流 JSON、
节点 ID)和商用 API 差得最远。

---

## 一、你需要提供三样东西

| 项 | 说明 | 怎么拿 |
| --- | --- | --- |
| 服务地址 | ComfyUI 的 HTTP 地址 | 例如 `http://comfyui:8188`,与后端同网段 |
| 工作流 JSON | 真实可跑的试穿工作流 | ComfyUI 界面里 **Save (API Format)**,不是普通 Save |
| 节点 ID | 各输入节点在该 JSON 里的编号 | 见下节 |

> **Save (API Format) 很关键。** 普通 Save 存的是画布布局,带 UI 坐标,
> `/prompt` 接口不认。API Format 才是 `{节点ID: {class_type, inputs}}` 的形状。

## 二、怎么找到节点 ID

导出 API Format JSON 后,它长这样:

```json
{
  "12": { "class_type": "LoadImage", "inputs": { "image": "garment.png" } },
  "20": { "class_type": "CLIPTextEncode", "inputs": { "text": "studio lighting" } },
  "25": { "class_type": "KSampler", "inputs": { "seed": 42, "steps": 20 } },
  "40": { "class_type": "SaveImage", "inputs": { "images": ["31", 0] } }
}
```

外层的键就是节点 ID。按语义找出这六个:

| 配置项 | 找哪个节点 | 典型 class_type |
| --- | --- | --- |
| `garment_image` | 载入商品图的那个 | `LoadImage` |
| `model_image` | 载入模特图的那个 | `LoadImage` |
| `positive_prompt` | 正向提示词 | `CLIPTextEncode` |
| `negative_prompt` | 负向提示词 | `CLIPTextEncode` |
| `seed` | 采样器 | `KSampler` / `KSamplerAdvanced` |
| `output` | 保存输出的那个 | `SaveImage` |

两个 `LoadImage` 和两个 `CLIPTextEncode` 很容易搞混 ——
在 ComfyUI 界面里点中节点、看它连到哪里,比对着 JSON 猜快得多。

## 三、配置

```bash
cd comfyui
cp config.example.yaml config.yaml
# 填 base_url 与六个节点 ID,把工作流 JSON 放进 workflows/
```

```yaml
comfyui:
  base_url: http://comfyui:8188
  workflow_file: workflows/virtual_tryon.json
  timeout_seconds: 300
  poll_interval_seconds: 2
  nodes:
    garment_image: "12"
    model_image: "13"
    positive_prompt: "20"
    negative_prompt: "21"
    seed: "25"
    output: "40"
```

同时在 `.env` 里:

```bash
COMFYUI_BASE_URL=http://comfyui:8188
COMFYUI_CONFIG_FILE=./comfyui/config.yaml
# 结果图要从内网下载,必须显式放行这个主机(需求第十九章的 SSRF 防护)
DOWNLOAD_ALLOWED_HOSTS=comfyui
```

**最后那条容易漏。** 系统默认拒绝从内网地址下载结果图,不放行的话
ComfyUI 生成成功、下载会被挡住,报 `RESULT_DOWNLOAD_FAILED`。

## 四、为什么节点 ID 必须来自配置

需求第六章的硬性要求,理由很实在:工作流会变。换个模型、加个 ControlNet、
调一下节点顺序,ID 就全变了。如果这些数字散落在业务代码里,
每次调工作流都要改 Python、重新发版、重跑测试。

放在配置里,调工作流只是改一个 YAML。业务代码只认语义名
(`garment_image` / `seed` / ...),永远不认数字。

`app/providers/comfyui.py` 已经按这个约定写好:
它读配置拿到映射表,再用映射表去替换工作流 JSON 里对应节点的输入。
**没有一处 `"12"` 这样的字面量。**

## 五、接入时代码要补什么

骨架已完成的部分:配置类、`ComfyUIConfig` 校验、能力声明、
`is_configured()`(缺地址或缺节点映射都算未配置)、连接测试、
清晰的 `NotConfiguredError`(会告诉你**具体缺哪几个槽位**)。

待补的部分,都在 `app/providers/comfyui.py` 里标了 `TODO`:

1. **上传图片** → `POST /upload/image`,拿到 ComfyUI 侧的文件名
2. **替换节点输入** → 把上传后的文件名、提示词、seed、尺寸写进工作流 JSON 副本
3. **提交** → `POST /prompt`,拿 `prompt_id`
4. **查询** → `GET /queue` 与 `GET /history/{prompt_id}`
5. **下载输出** → `GET /view?filename=...&subfolder=...&type=output`
6. **取消** → `POST /interrupt`(ComfyUI 支持,这是它比 FASHN 强的地方)

具体端点与字段**必须对着你那版 ComfyUI 的实际行为确认**,不同版本有差异。
和 FASHN 一样,拿到确认前不写死任何字段名。

## 六、接入后怎么验

按 FASHN 那次的顺序:

```bash
# 1) 连通性(不出图)
curl -X POST http://localhost:8000/api/providers/comfyui/test

# 2) 与 Mock 基线对比,看它相对基线好在哪
make baseline SKU=SW-001-BLK-S P=mock,comfyui

# 3) 端到端闭环
docker compose exec backend python -m app.scripts.smoke_test --provider comfyui
```

## 七、常见问题

| 现象 | 原因 |
| --- | --- |
| 一直报"未配置" | `config.yaml` 没建,或六个节点 ID 没填全 —— 连接测试会列出缺哪几个 |
| 提交返回 400 | 工作流不是 API Format 导出的;或节点 ID 对不上当前工作流 |
| 生成成功但下载失败 | `DOWNLOAD_ALLOWED_HOSTS` 没放行 ComfyUI 主机 |
| 输出图拿不到 | `output` 指的不是 `SaveImage`;`PreviewImage` 不落盘,拿不到文件 |
| 换了工作流就全崩 | 节点 ID 变了,改 `config.yaml` 即可,不用改代码 —— 这正是配置化的意义 |
