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
# 和 gitignore 的语法不一样。两份清单要一起改 —— `backend/tools/verify_delivery.py`
# 里有一条检查盯着它们不许分叉(那 9 条原来是 `test_delivery_hygiene.py`,
# 早已并进 verify_delivery)。

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
# `verify_delivery.py` 会当场红 —— 那条检查要求根目录有 .gitignore,
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
  .vite-cache
  .import_linter_cache
  .tmp*
  '*.egg-info'
  .venv
  venv
  node_modules
  test-results
  playwright-report
  patch
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

# 目录本身要跟着交付走,但**里面的图片不进包**。
#
# `data/` 是本机随手放素材的地方。上一版交付包里躺着一张 5.8 MB 的 `data/s1.jpg`,
# 占了整个包的一半 —— 而它既不在 `.gitignore` 里、也没有任何代码或文档引用它,
# 也就是说**没有任何机制拦着它**。这类东西不像凭据那样危险,但它有另一种代价:
# 交付包每次都胖一倍,而收包的人无从判断这张图算不算交付内容。
#
# **排的是扩展名,不是整个 `data/`。** 目录里将来可能放说明文件、参数样例,
# 那些该跟着走;把整棵子树排掉的话,下一个往 data/ 放 README 的人会发现
# 它神秘消失,而脚本不会说任何话。
#
# 大小写在这里是个真陷阱:Info-ZIP 的 `-x` 在 Unix 上**大小写敏感**,
# 写成 `data/*.jpg` 的话 `data/S1.JPG` 照样进包,而复验侧如果也照抄同一个
# 模式,两边会一起漏 —— 那正是这个脚本第 2 步要防的「门禁看起来在跑」。
# 所以扩展名统一展开成 `[jJ][pP][gG]` 这样的字符类(见 `ext_any_case`),
# zip 的 -x 与 grep -E 都认这个写法,大小写这件事只处理一次。
IMAGE_FREE_DIRS=(
  data                # 本机素材暂存,不是交付内容
)

# 判据是扩展名。列表宁可长一点 —— 漏一种的表现是那一种照样进包,
# 而包变大这件事没有人会去查是哪个文件干的。
IMAGE_EXTENSIONS=(
  jpg
  jpeg
  png
  gif
  bmp
  webp
  tif
  tiff
  heic
  heif
  avif
  svg
  psd
  ico
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
  # 运行期日志。`backend/.api-stdout.log` / `.api-stderr.log` 连着两个交付包
  # 出去过:里面是 uvicorn 的完整请求行与异常栈,含图片绝对路径、上游 URL 与
  # 查询串。它和 `data/` 那张图同类 —— 没有任何机制拦着它,既不在 .gitignore
  # 也没有代码引用。**basename 匹配**,任意层级都拦。
  '*.log'
  # Claude Code 的个人权限配置。里面是**开发机的绝对路径**(实测带出过
  # `/c/Users/<user>/.claude/…` 这样的 Windows 用户名与本地路径),对收包方
  # 没有任何用处,而且泄露了打包者的机器信息。它不像凭据那样致命,但和
  # `data/` 那张 5.8MB 图同类:没有任何机制拦着它 —— 它既不在 .gitignore、
  # 也没有代码或文档引用。**basename 匹配**,任意层级都拦(约定上
  # `settings.local.json` 恒为个人文件,共享设置走 `settings.json`)。
  'settings.local.json'
)

# 留在仓库里是对的,**只是不随交付包走**。
#
# 和上面那组的区别只有一条,而那一条要紧:`FORBIDDEN_FILES` 里的东西**出去了
# 就是事故**(凭据、运行期日志、开发机绝对路径);这一组出去了什么也不会发生,
# 它只是白占体积。混进上面那组会让"禁品"这个词贬值 —— 一份展示页和一把主密钥
# 摆在同一张清单上,下一个人读到告警时不知道该按哪一种反应,而那两种反应
# 一个是轮换密钥、一个是耸耸肩。
#
# 判据是「它能不能从仓库里的别的东西重新做出来」。能,就归这里。
NOT_SHIPPED_FILES=(
  'docs/overview.html'   # 架构图册的单文件展示版,由 docs/assets 的十四张图组装
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

# `jpg` -> `[jJ][pP][gG]`。zip 的 -x 与 grep -E 都认字符类,所以"大小写"
# 只在这里处理一次,不在排除侧和复验侧各写一遍(那两份一定会分叉)。
ext_any_case() {
  local e="$1" out="" c i
  for (( i = 0; i < ${#e}; i++ )); do
    c="${e:i:1}"
    out+="[${c,,}${c^^}]"
  done
  printf '%s' "$out"
}

EXCLUDES=()
FORBIDDEN=()
# 「不随包」的复验模式单独一份。和 FORBIDDEN 共用一个数组的话,报错会说
# 「交付包里出现禁止的内容」—— 对一份展示页那句话过重,而过重的告警会被
# 学着忽略,连带下一次真的凭据告警一起。
NOT_SHIPPED=()

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

for d in "${IMAGE_FREE_DIRS[@]}"; do
  for e in "${IMAGE_EXTENSIONS[@]}"; do
    pat="$(ext_any_case "$e")"
    # Info-ZIP 的 `*` 会跨越 `/`,所以一条就盖住 data/ 下的任意深度。
    # 目录条目与非图片文件不受影响 —— 那是这条规则和 CONTENT_ONLY_DIRS 的区别。
    EXCLUDES+=( "$d/*.$pat" "*/$d/*.$pat" )
    FORBIDDEN+=( "(^|/)$(glob_to_re "$d")/.*\.$pat\$" )
  done
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

for f in "${NOT_SHIPPED_FILES[@]}"; do
  EXCLUDES+=( "$f" "*/$f" )
  NOT_SHIPPED+=( "(^|/)$(glob_to_re "$f")\$" )
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
# 会把人引向完全错误的方向。这里统一归一化,两侧共用同一份清单。
#
# 清单**落盘后再比对,所有 grep 都读文件,不吃管道**。这不是风格问题:
# 2026-08-10 两轮打包各撞到一次"必备文件被误报缺失,而它就在清单里",
# 复现率约 3/12,根因是 `printf '%s\n' "$LISTING" 喂给 grep -qxF 的那条
# 管道 —— `grep -q` 在清单**前几行**命中后立即退出,printf 还没写完就会
# 收到 SIGPIPE(退出码 141),而 `set -o pipefail` 把 141 当成整条管道的
# 失败,`if !` 再把它读成"没找到"。当时的三个观察逐一对上:被误报的
# 全是排在清单最前面的文件(命中越早,printf 越可能还没写完);失败时
# 条目数与成功时完全相同(清单本来就是全的);unzip -t 全过(包本来就是
# 好的)。同形复现 400 次得 38 次假阴性、退出码全是 141;命中行挪到清单
# 末尾 0/400;改成 grep 读文件 0/400。细节与守卫见 DECISIONS §3.69。
# 顺带,清单文件从"失败才落盘"变成每次都落盘 —— 包发出去之后,它是唯一
# 能回答"当时打进去了什么"的东西。
LISTING_FILE="$OUT.listing.txt"
unzip -Z1 "$OUT" | sed 's|^\./||' > "$LISTING_FILE"
# 只放行仓库声明的两个模板路径。不能按 basename 豁免,否则任意子目录放一份
# `.env.example` 都能绕过凭据复验。
LISTING_WITHOUT_ENV_EXAMPLES_FILE="$(mktemp)"
trap 'rm -f "$LISTING_WITHOUT_ENV_EXAMPLES_FILE"' EXIT
grep -vxF -e "${ENV_EXAMPLES[0]}" -e "${ENV_EXAMPLES[1]}" "$LISTING_FILE" > "$LISTING_WITHOUT_ENV_EXAMPLES_FILE" || true
FAILED=0

for pattern in "${FORBIDDEN[@]}"; do
  HITS="$(grep -E "$pattern" "$LISTING_WITHOUT_ENV_EXAMPLES_FILE" || true)"
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

# 「不随包」这一组同样要**打完再验一遍**。理由和禁品那组一模一样:只写排除
# 规则的话,规则写错了没有任何征兆 —— 包照样生成,只是又胖了回去,而没有人
# 会去查是哪个文件干的。
for pattern in "${NOT_SHIPPED[@]}"; do
  HITS="$(grep -E "$pattern" "$LISTING_WITHOUT_ENV_EXAMPLES_FILE" || true)"
  if [ -n "$HITS" ]; then
    echo "!! 交付包里出现不随包的文件(模式 $pattern):"
    while IFS= read -r hit; do
      printf '     %s\n' "$hit"
    done <<< "$HITS"
    echo "   它们留在仓库里是对的,只是不占交付体积 —— 修排除清单,别手工从包里删。"
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
  '.gitattributes'
  '.gitignore'
  '.github/workflows/ci.yml'
  'backend/tools/verify_delivery.py'
  'Makefile'
  # 行尾门禁的**唯一实现**。它必须跟着包走:收包方从解出来的这棵树重新打包
  # 时,下面那道门禁要能找到它 —— 找不到就 fail-closed,而"包里没带"是一个
  # 完全可以避免的 fail-closed。
  'tools/normalize_eol.py'
)
# 这一条报出来的时候,有两种完全不同的可能,而报错必须能分开它们:
#
#     真的没打进去     排除清单写宽了、或者文件真的不在工作树里
#     清单读短了       `unzip -Z1` 没有把条目全吐出来
#
# 2026-08-10 之前还有第三种:比对本身出假阴性 —— 文件在清单里而 grep 说
# 没有。那一种已经定位并修掉(SIGPIPE × pipefail,见上面复验一节的注释与
# DECISIONS §3.69),这两条诊断当时把范围收窄到了比对路径上,是它们该有的
# 功劳,所以保留。前两种的修法相反 —— 前者改脚本,后者重打一次 —— 失败时
# 把判断需要的东西一起打出来,而不是让下一个人从一行「缺少必备文件」开始猜。
report_listing_health() {
  echo "   诊断:清单读到 $(grep -c . "$LISTING_FILE" || true) 条条目"
  if unzip -t "$OUT" >/dev/null 2>&1; then
    echo "   诊断:压缩包自身完整(unzip -t 通过)—— 那么问题在排除清单或工作树"
  else
    echo "   诊断:压缩包**本身**校验不过 —— 重打一次;仍然如此再查磁盘与 zip 版本"
  fi
  # 包失败会被删掉;清单在复验开始前就已落盘,是唯一还能回看的证据。
  echo "   诊断:清单在 $LISTING_FILE(包会被删,这份不会)"
}

for path in "${REQUIRED[@]}"; do
  if ! grep -qxF "$path" "$LISTING_FILE"; then
    echo "!! 交付包缺少必备文件:$path"
    if [ -e "$path" ]; then
      echo "   注意:这个文件**在工作树里存在** —— 它是被排除掉了,或者清单没读全"
    fi
    report_listing_health
    FAILED=1
  fi
done

# ---------------------------------------------------------------- 行尾复验
#
# 这一道和上面两道是同一个思路的第三次应用:**验产物的真实字节,不验声明**。
#
# `.gitattributes` 第一行写着 `* text=auto eol=lf`,而实测 a55 那个交付包
# 832 个文件里 711 个是 CRLF 或混合 —— 包括 `.github/workflows/ci.yml`,
# 它**明明在白名单里**。原因不在打包脚本:`.gitattributes` 是检出期规则,
# 工作树比规则老的时候 git 不回头重写,而这两个脚本逐字节复制工作树。
# 于是规则声明了、没有生效、也没有任何东西发现这件事,包照样打印 `==> OK`。
#
# 所以判据只能是**解开包之后那些字节到底长什么样**,而不是仓库里那份声明。
# 三处会真的坏掉:compose 的 `env_file` 把 `\r` 读进密码、Dockerfile 的
# 反斜杠续行断掉、`.dockerignore` 的条目失配。理由全文在 `.gitattributes` 顶部。
#
# 判定本身在 `tools/normalize_eol.py` 里,不在这里重写一遍 —— 这个脚本
# 开头两百行讲的就是清单分叉,而"行尾判定在 bash 和 PowerShell 里各写一份"
# 分叉出来的表现恰好是「Linux 打的包干净、Windows 打的包带 CRLF」,
# 正是这道门禁要防的场景本身。
EOL_CHECKER="$ROOT/tools/normalize_eol.py"
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ] || [ ! -f "$EOL_CHECKER" ]; then
  # **fail-closed,不是跳过。**
  #
  # 跳过的代价是一个带 CRLF 的包被打出来、打印 OK、发出去,而"这台机器上
  # 没有 python3"这件事没有任何人会看见 —— 那正是这个脚本存在的理由:
  # 一个看起来在跑的门禁比没有门禁更糟。仓库本来就要求 Python 才能跑测试
  # 与交付自检,打包机上没有它是环境问题,不是可以静默放行的理由。
  echo "!! 无法执行行尾复验,包不予生成:"
  [ -z "$PYTHON_BIN" ] && echo "     找不到 python3/python"
  [ ! -f "$EOL_CHECKER" ] && echo "     缺少 $EOL_CHECKER"
  FAILED=1
else
  EOL_DIR="$(mktemp -d)"
  trap 'rm -f "$LISTING_WITHOUT_ENV_EXAMPLES_FILE"; rm -rf "$EOL_DIR"' EXIT
  if unzip -q -o "$OUT" -d "$EOL_DIR"; then
    if ! "$PYTHON_BIN" "$EOL_CHECKER" --check "$EOL_DIR" --quiet; then
      echo "!! 上面这些文件是**从刚打好的包里解出来的**,不是工作树里的。"
      echo "   工作树先跑一次 \`$PYTHON_BIN tools/normalize_eol.py --write .\`,再重打。"
      FAILED=1
    fi
  else
    echo "!! 行尾复验解不开这个包 —— 包本身有问题,重打一次"
    FAILED=1
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  rm -f "$OUT"
  echo
  echo "包已删除。修好排除清单再打一次 —— 不要手工从包里删文件后再发出去,"
  echo "下一次还会是同样的结果。"
  exit 1
fi

# 目录条目不算文件,否则报出来的数字比解包看到的多一截。
COUNT="$(grep -cv '/$' "$LISTING_FILE" || true)"
SIZE="$(du -h "$OUT" | cut -f1)"
echo "==> OK  $COUNT 个文件,$SIZE"
echo "    $OUT"
echo "    清单 $LISTING_FILE"
