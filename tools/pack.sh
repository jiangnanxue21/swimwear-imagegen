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
# ## 为什么禁品只声明一次
#
# 上一版里排除清单(zip 的 -x)和复验清单(grep 的模式)是分开手写的两份,
# 于是它们分叉了:`.env.local`、`comfyui/config.yaml`、`.git/`、`*.pyc`
# 都只出现在排除侧,复验侧完全没查 —— 也就是说这些东西一旦真的进了包,
# 脚本会打印 `==> OK`。这正是第 2 步想防的那种「门禁看起来在跑」。
#
# 现在两份都由下面 FORBIDDEN_DIRS / FORBIDDEN_FILES / CONTENT_ONLY_DIRS
# 生成,改一处两边同时生效,不存在只改一边的写法。
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

# 版本名会直接拼进输出路径。不校验的话 `tools/pack.sh ../../foo` 能把包
# 写到仓库外面去,或者带空格的版本名让后面的 du/unzip 全部错位。
if [[ ! "$VERSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "!! 版本名只允许 [A-Za-z0-9._-]:$VERSION" >&2
  exit 2
fi

OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/swimwear-imagegen-${VERSION}.zip"

mkdir -p "$OUT_DIR"
rm -f "$OUT"

# ---------------------------------------------------------------- 禁品声明
#
# 与 .gitignore 同源,但**不能**直接读 .gitignore 代替:交付包比仓库多排
# 一些东西(dist/ 自己、历史交接文档不排但要单独说),而且 zip 的 -x 语法
# 和 gitignore 的语法不一样。两份清单要一起改 —— `test_delivery_hygiene.py`
# 里有一条测试盯着它们不许分叉。

# 整棵子树都不进包,根层级和子层级都要排。
#
# 上一版的 P0 就在这里:`.idea` `.vscode` `.pytest_cache` `.ruff_cache`
# `coverage` 这五个只写了 `*/X/*`,**漏了根层级的 `X/*`**。而复验清单里
# 这五个一个都没有。于是仓库根下的 .idea/workspace.xml、coverage/、
# .pytest_cache/ 原封不动进了包,脚本照样打印 `==> OK` —— 实测复现过。
#
# 单独手写四种形态迟早还会漏一种,所以下面统一由目录名生成。
#
# 注意 `.gitignore` 和 `.github/` **不在**这里:它们必须跟着交付包走。
# 少了 .gitignore,解包的人第一次 `git init` 就又回到 P0-1 的起点;而
# `test_delivery_hygiene.py` 会当场红 —— 那条测试要求根目录有 .gitignore,
# 它检查的正是解包之后的这棵树。下面的模式用 (/|$) 收尾,`.gitignore`
# 和 `.github` 都不会被 `.git` 误伤,REQUIRED 复验也会兜住这一点。
FORBIDDEN_DIRS=(
  .secrets            # 凭据 —— 这一组是这个脚本存在的理由
  .git                # 版本控制与编辑器
  .idea
  .vscode
  __pycache__         # 构建产物
  .pytest_cache
  .ruff_cache
  .venv
  venv
  node_modules
  dist
  coverage
)

# 与上面的区别只在复验侧的松紧:这里允许包中出现一个空的目录条目。
#
# 实测 Info-ZIP 的 `-x 'storage/*'` 里 `*` 可以匹配空串,所以连 `storage/`
# 这个条目也一并排掉了 —— 也就是说当前环境下两组行为其实一样。保留这个
# 分组是因为不是所有 zip 实现都这样,而 storage/ 是个挂载点、留一个空条目
# 无害;.secrets/ 则相反,空目录名本身就是信息,所以走上面那组。
CONTENT_ONLY_DIRS=(
  storage             # 运行期数据:真实素材、产出图、数据库卷
)

# 单个文件。不含 / 的按 basename 匹配(任意层级),含 / 的按仓库根锚定。
FORBIDDEN_FILES=(
  '.env'                  # 上一版复验只有 `(^|/)\.env$`,$ 锚点管不到
  '*.env'                 # production.env 这类 —— 实测这条真的漏出去过
  '*.key'
  '*.pem'
  'comfyui/config.yaml'   # 上一版复验完全没查这一条
  '*.pyc'
  '*.pyo'
  '*.tsbuildinfo'
  '.DS_Store'
)

# `.env.example` 是零凭据模板,解包后的配置契约与 `make init` 都依赖它。
# 不能把 `.env.*` 当普通 basename glob:Info-ZIP 没有可靠的“排除但反选一个”语义。
# 所以先排除整类,再只把这一个明确允许的文件补回；复验侧同样只豁免精确路径。
ENV_DOTFILE_GLOB='.env.*'
ENV_EXAMPLES=(
  '.env.example'
  'frontend/.env.example'
)

# ---------------------------------------------------------------- 清单生成
#
# glob → ERE。`*` 只匹配单层,和 basename 语义一致。
glob_to_re() {
  local g="$1" out="" c i
  for (( i = 0; i < ${#g}; i++ )); do
    c="${g:i:1}"
    case "$c" in
      '*')  out+='[^/]*' ;;
      '?')  out+='[^/]'  ;;
      '.'|'+'|'('|')'|'['|']'|'{'|'}'|'^'|'$'|'|'|'\') out+="\\$c" ;;
      *)    out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

EXCLUDES=()
FORBIDDEN=()

for d in "${FORBIDDEN_DIRS[@]}"; do
  # 四种形态都要排:根下的内容、根下的目录条目、子层级的内容、子层级的目录条目。
  # `*/X/...` 这两条顺带兜住某些 Info-ZIP 版本保留的 `./` 前缀。
  EXCLUDES+=( "$d/*" "$d/" "*/$d/*" "*/$d/" )
  FORBIDDEN+=( "(^|/)$(glob_to_re "$d")(/|\$)" )
done

for d in "${CONTENT_ONLY_DIRS[@]}"; do
  EXCLUDES+=( "$d/*" "*/$d/*" )
  # 目录条目本身是允许的,所以复验要求后面至少还有一个字符。
  FORBIDDEN+=( "(^|/)$(glob_to_re "$d")/.+" )
done

for f in "${FORBIDDEN_FILES[@]}"; do
  if [[ "$f" == */* ]]; then
    EXCLUDES+=( "$f" "*/$f" )
    FORBIDDEN+=( "(^|/)$(glob_to_re "$f")\$" )
  else
    EXCLUDES+=( "$f" "*/$f" )
    FORBIDDEN+=( "(^|/)$(glob_to_re "$f")\$" )
  fi
done

EXCLUDES+=( "$ENV_DOTFILE_GLOB" "*/$ENV_DOTFILE_GLOB" )
FORBIDDEN+=( '(^|/)\.env\.[^/]+$' )

# ---------------------------------------------------------------- 打包
#
# -X 去掉 UID/GID 等本机 extra field。默认 zip 会把打包者的数字 uid/gid
# 写进每一个条目 —— 对外交付时这是白送的可识别元数据。
echo "==> 打包 $OUT"
zip -q -r -X "$OUT" . -x "${EXCLUDES[@]}"
zip -q -X "$OUT" "${ENV_EXAMPLES[@]}"

# ---------------------------------------------------------------- 打完再验一遍
#
# 这些模式一旦在包里命中,就是 P0。不 warn,直接删包退出 ——
# 一个「有警告但仍然生成了」的包会被发出去,这一点已经有先例。
#
# 先把 `./` 前缀削平。上一版 FORBIDDEN 用 (^|/) 容忍了它,REQUIRED 却用
# grep -qx 精确比对 —— 同一份清单两套前缀假设。某些平台的 Info-ZIP 保留
# `./` 时,四条必备文件会被全判为缺失,而报错信息("缺少必备文件")
# 会把人引向完全错误的方向。这里统一归一化,两侧共用同一份 LISTING。
LISTING="$(unzip -Z1 "$OUT" | sed 's|^\./||')"
# 只放行仓库声明的两个模板路径。不能按 basename 豁免,否则任意子目录放一份
# `.env.example` 都能绕过凭据复验。
LISTING_WITHOUT_ENV_EXAMPLES="$(
  printf '%s\n' "$LISTING" |
    grep -vxF -e "${ENV_EXAMPLES[0]}" -e "${ENV_EXAMPLES[1]}" || true
)"
FAILED=0

for pattern in "${FORBIDDEN[@]}"; do
  HITS="$(printf '%s\n' "$LISTING_WITHOUT_ENV_EXAMPLES" | grep -E "$pattern" || true)"
  if [ -n "$HITS" ]; then
    echo "!! 交付包里出现禁止的内容(模式 $pattern):"
    # 走 while read 而不是 printf '%s\n' $HITS —— 后者未加引号,
    # 文件名带空格会被拆行,带 * 会被 glob 展开,报错信息本身就失真了。
    while IFS= read -r hit; do
      printf '     %s\n' "$hit"
    done <<< "$HITS"
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
  '.env.example'
  'frontend/.env.example'
  '.gitignore'
  '.github/workflows/ci.yml'
  'backend/tools/verify_delivery.py'
  'Makefile'
)
for path in "${REQUIRED[@]}"; do
  if ! printf '%s\n' "$LISTING" | grep -qxF "$path"; then
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

# 目录条目不算文件,否则报出来的数字比解包看到的多一截。
COUNT="$(printf '%s\n' "$LISTING" | grep -cv '/$' || true)"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "==> OK  $COUNT 个文件,$SIZE"
echo "    $OUT"
