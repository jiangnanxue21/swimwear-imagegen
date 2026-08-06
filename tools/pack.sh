#!/usr/bin/env bash
#
# 交付打包(评审 P0-1 / T-6)。
#
# ## 为什么打包要有脚本
#
# 主密钥连着两个交付包出去过,第二次那个包和它毫无关系。根因不是谁忘了 ——
# 是每一次打包都是手打的一条 `zip -r`,而手打的命令**没有记忆**:
# 上一次记得加 `-x`,下一次换个人、换个主题就没了。
#
# 所以这里做两件事,顺序不能反:
#
#     1. 按白名单/黑名单打包
#     2. **打完之后再解开看一遍**,发现禁品直接删包并退出非零
#
# 第 2 步是重点。只做第 1 步的话,排除规则写错了(比如少一个星号、
# 路径前缀不对)不会有任何征兆 —— 包照样生成,照样发出去,
# 而所有人都以为规则在工作。这和 `eslint.config.js` 里那条
# 「一个没人跑的门禁比没有门禁更糟」是同一件事。
#
# ## 用法
#
#     tools/pack.sh                    # → dist/swimwear-imagegen-<版本>.zip
#     tools/pack.sh a20                # 指定版本名
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-$(date +%Y%m%d-%H%M)}"
OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/swimwear-imagegen-${VERSION}.zip"

mkdir -p "$OUT_DIR"
rm -f "$OUT"

# ---------------------------------------------------------------- 排除清单
#
# 与 .gitignore 同源,但**不能**直接读 .gitignore 代替:交付包比仓库多排
# 一些东西(dist/ 自己、历史交接文档不排但要单独说),而且 zip 的 -x 语法
# 和 gitignore 的语法不一样。两份清单要一起改 —— `test_delivery_hygiene.py`
# 里有一条测试盯着它们不许分叉。
EXCLUDES=(
  # 凭据 —— 这一组是这个脚本存在的理由
  '.secrets/*' '*/.secrets/*'
  '.env' '*/.env' '.env.local' '*/.env.local'
  '*.key' '*.pem'
  'comfyui/config.yaml'

  # 运行期数据(真实素材、产出图、数据库卷)
  'storage/*' '*/storage/*'

  # 构建产物
  '*/__pycache__/*' '__pycache__/*' '*.pyc' '*.pyo'
  '*/.pytest_cache/*' '*/.ruff_cache/*'
  '*/node_modules/*' 'node_modules/*'
  '*/dist/*' 'dist/*'
  '*.tsbuildinfo'
  '*/coverage/*'

  # 版本控制与编辑器
  #
  # 注意 `.gitignore` **不在**这里:它必须跟着交付包走。少了它,
  # 解包的人第一次 `git init` 就又回到 P0-1 的起点;而且
  # `test_delivery_hygiene.py` 会当场红 —— 那条测试要求根目录有 .gitignore,
  # 它检查的正是解包之后的这棵树
  '.git/*' '*/.git/*'
  '.DS_Store' '*/.DS_Store'
  '*/.idea/*' '*/.vscode/*'
)

echo "==> 打包 $OUT"
zip -q -r "$OUT" . -x "${EXCLUDES[@]}"

# ---------------------------------------------------------------- 打完再验一遍
#
# 这些模式一旦在包里命中,就是 P0。不 warn,直接删包退出 ——
# 一个「有警告但仍然生成了」的包会被发出去,这一点已经有先例。
FORBIDDEN=(
  '\.secrets/'
  '(^|/)\.env$'
  '\.key$'
  '\.pem$'
  '\.tsbuildinfo$'
  '(^|/)node_modules/'
  '(^|/)__pycache__/'
  '(^|/)storage/.+'
)

LISTING="$(unzip -Z1 "$OUT")"
FAILED=0
for pattern in "${FORBIDDEN[@]}"; do
  if HITS="$(printf '%s\n' "$LISTING" | grep -E "$pattern" || true)"; [ -n "$HITS" ]; then
    echo "!! 交付包里出现禁止的内容(模式 $pattern):"
    printf '     %s\n' $HITS
    FAILED=1
  fi
done

# ---------------------------------------------------------------- 必备文件复验
#
# 只查禁品、不查必备品的话,漏打 `.github/` 或 `.gitignore` 的包照样
# 「验证通过」发出去 —— a45-batch10 的交付包正是这样:解包后交付自检 9/13、
# 纯测试当场 FileNotFoundError,而打包这一步没有任何征兆。
# 清单与 `verify_delivery.py` 的硬失败项对齐:少了它们,解包侧的门禁必红。
REQUIRED=(
  '.gitignore'
  '.github/workflows/ci.yml'
  'backend/tools/verify_delivery.py'
  'Makefile'
)
for path in "${REQUIRED[@]}"; do
  if ! printf '%s\n' "$LISTING" | grep -qx "$path"; then
    echo "!! 交付包缺少必备文件:$path"
    FAILED=1
  fi
done

if [ "$FAILED" -ne 0 ]; then
  rm -f "$OUT"
  echo
  echo "包已删除。修好排除清单再打一次 —— 不要手工从包里删文件后再发出去,"
  echo "下一次还会是同样的结果。"
  exit 1
fi

COUNT="$(printf '%s\n' "$LISTING" | wc -l | tr -d ' ')"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "==> OK  $COUNT 个文件,$SIZE"
echo "    $OUT"
