# 部署与运维

面向"接手这套系统、要把它跑起来并保持它跑着"的工程师。
开发环境启动看 README,这里说部署、排查和日常运维。

第一到九节假设 Linux。**在 Windows 或 macOS 上部署,先读第十节** —— 
服务本身没有差异(都在容器里),但换行符、宿主机网络和缺少 `make` 这三处
会实实在在地卡住你,而其中两处的报错不会指向真正的原因。

---

## 一、组件与依赖

| 组件 | 作用 | 挂了会怎样 |
| --- | --- | --- |
| `backend` | FastAPI,处理 HTTP | 后台打不开;已排队的任务仍会被 worker 跑完 |
| `worker` | Celery,跑生成流水线 | 接口正常、任务永远停在 `QUEUED` |
| `postgres` | 全部业务数据 | 一切停摆 |
| `redis` | Celery broker | 新任务派发不出去,接口仍能创建任务 |
| 存储 | 素材与成品图 | 图片打不开;生成会在落盘时失败 |

**worker 挂掉是最隐蔽的故障**:接口一切正常,创建任务返回 201,
只是永远不动。`make worker-ping` 是判断它死活最快的方式。

### 宿主机上的最低版本要求

| 工具 | 最低版本 | 为什么 |
| --- | --- | --- |
| Docker Compose | **2.24.4** | `docker-compose.prod.yml` 用了 `!override` 标签 |

`docker compose version` 查。低于这个版本,`docker compose -f docker-compose.yml
-f docker-compose.prod.yml up` 会直接报未知标签而拒绝启动。

**那是个好失败,不要绕过它。** `!override` 在这里防的是一件不会自己暴露的事:

compose 合并两份文件时,同一服务的 `volumes` 是**合并**而不是替换 ——
容器内路径相同的条目被覆盖,其余原样保留。基座 `docker-compose.yml` 给
`backend` / `worker` / `beat` 挂了 `./backend:/app`(开发时热加载用),
而生产 overlay 里没有任何一条指向 `/app`。少了 `!override`,那行 bind mount
会一路活到生产,后果分两种:

- **部署机上没有源码目录** —— `/app` 被一个空目录盖住,三个服务全起不来。
  现象是 `ModuleNotFoundError: app`,而镜像里明明有那个包
- **部署机上有源码目录** —— 更糟:镜像里那份构建产物被工作区源码盖掉,
  跑的是未经构建、可能带着本地未提交改动的代码。**它能起来**,所以没有人会发现

所以如果现场遇到未知标签的报错,正确处置是升级 compose,
**不是**把 `!override` 删掉 —— 删掉正好回到上面那两种状态。

## 二、首次部署

```bash
cp .env.example .env
# 至少改这三项:POSTGRES_PASSWORD、PUBLIC_BASE_URL、SECRET 类配置
# 再加浏览器登录那三项 —— 非本机环境不配就起不来,见下一节
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
make migrate
make seed              # 可选:导入示例商品
make smoke             # 端到端冒烟,一分钟内给出结论
```

**上面用的是类生产编排**(前端构建产物由 Nginx 托管、监听 `127.0.0.1:8080`,
backend 不再对外发布端口)。只跑 `docker compose up -d --build` 起的是 Vite
开发服务器,那只适合开发与 UAT 的第一轮。

> **`APP_ENV` 不是 local/dev/development 时,`ADMIN_PASSWORD` /
> `OPERATOR_PASSWORD` / `AUTH_SESSION_SECRET` 三项必填,配不全后端直接起不来。**
> 生产 overlay 里这三项写的是 `${KEY:?}`,所以 compose 会在**创建容器之前**
> 就退出并把缺哪一项打在终端上 —— 而不是让容器起来又反复重启。
> 三项的含义、怎么生成密钥、换密钥等于全员登出,见 README「浏览器登录」一节。

`make smoke` 通过就意味着:健康检查、商品与素材、异步任务、评分分档、
多尺寸输出、导出、仪表盘这条链路全部可用。

## 三、必须改掉的默认值

`.env.example` 是**开发**默认值,直接上生产会出事:

| 变量 | 开发默认 | 生产要求 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | 明文弱口令 | 改掉,并用 secret 管理而不是 `.env` |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | 改成真实域名,否则导出的图片 URL 无法访问 |
| `STORAGE_BACKEND` | `local` | 建议 `s3`;本地存储由后端进程直接托管,不适合生产 |
| `LOG_LEVEL` | `INFO` | 保持 INFO;DEBUG 会打出请求体 |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | 必须是 false,否则生成会在 HTTP 请求里同步跑 |
| `DOWNLOAD_ALLOWED_HOSTS` | 空 | 只在自建 ComfyUI 时填,填了等于放行内网下载 |
| `ADMIN_PASSWORD` | 空 | **必填**,浏览器登录的管理员密码;不填则非本机环境起不来 |
| `OPERATOR_PASSWORD` | 空 | **必填**,运营账号密码,不能与上面相同 |
| `AUTH_SESSION_SECRET` | 空 | **必填**,至少 32 字符;多机部署各节点必须同一把,否则用户隔一次请求就掉线 |

## 四、存储:从本地换到对象存储

本地存储由后端 `/files` 直接托管,只适合开发。生产改法:

```bash
STORAGE_BACKEND=s3
S3_BUCKET=imagegen-prod
S3_ENDPOINT_URL=            # MinIO 才填,AWS 留空
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=ap-southeast-1
S3_PUBLIC_BASE_URL=https://cdn.example.com   # 走 CDN 时填
```

```bash
pip install -e ".[s3]"   # boto3 是可选依赖
```

**已有数据可以直接迁**:两种后端共用同一套 sha256 分片路径,
`mc mirror ./storage minio/imagegen-prod` 之后,数据库里的 `storage_path`
一个字都不用改。

## 五、日常运维

```bash
make logs              # 跟踪 backend 与 worker 日志
make worker-ping       # 验证 worker 存活
make psql              # 进数据库
make smoke             # 改完配置后跑一次,比读日志快
curl localhost:8000/api/health/ready   # 逐项报告 DB / Redis / 存储
```

日志是 JSON 行,带 `request_id`。排查一次请求:

```bash
docker compose logs backend | grep '"request_id": "<id>"'
```

密钥不会进日志 —— 键名命中 `api_key/secret/password/token/authorization/credential`
的值一律记为 `***`,由 `tests/pure/test_logging_redaction.py` 守住。

## 六、常见故障

| 现象 | 先查什么 |
| --- | --- |
| 任务永远停在 `QUEUED` | `make worker-ping`。worker 没起来或连不上 Redis |
| 任务停在 `PROVIDER_RUNNING` 很久 | FASHN 一次 20-120 秒属正常;超过轮询上限会抛 `NETWORK_TIMEOUT` |
| 大量 `RESULT_DOWNLOAD_FAILED` | 结果 URL 在内网而 `DOWNLOAD_ALLOWED_HOSTS` 没放行;或结果 URL 已过期 |
| 图片 404 | `PUBLIC_BASE_URL` 与实际域名不一致;或存储卷没挂上 |
| 后台能开但没有图 | `STORAGE_BACKEND=local` 时后端进程重启且用的是容器内临时目录,存储没做持久卷 |
| `PROVIDER_NOT_CONFIGURED` | Key 没进容器环境:`docker compose exec backend env \| grep FASHN` |
| 401 / `UnauthorizedAccess` | Key 失效,或复制时带了空格 |
| `OutOfCredits` | FASHN 余额用尽。**不会自动重试** —— 这是刻意的,重试也不会好 |
| 审核队列越积越多 | 看 `/dashboard` 的分档分布。A 档占比过低通常是素材问题,不是模型问题 |
| 重生请求被拒 409 | 撞到 `MAX_TOTAL_ROUNDS`(默认 10)。该 SKU 反复不达标,应改素材而不是继续烧额度 |
| 迁移失败 | 先 `alembic current` 看当前版本;链是 0001→0002→0003→0004 线性的 |

## 七、备份

要备份的只有两样:

1. **PostgreSQL** —— 全部业务数据、评分记录、审计日志
2. **存储** —— 素材原图与成品图

存储是内容寻址的(路径含 sha256),因此备份可以纯增量,不用担心覆盖。
**原始上传文件永不覆盖**是代码级不变量,由 `tests/pure/test_storage.py` 守住。

数据库里存的是 `storage_path` 相对路径,不含域名 —— 换域名或换存储后端
都不需要动数据。

## 八、伸缩

当前设计明确**没有**做高并发优化(需求第二十三章)。要扩容,按这个顺序:

1. **加 worker 副本** —— 一次 Celery 调用只跑一轮,天然可并行,直接加副本即可
2. **拆队列** —— 生成任务和评分任务混在一个队列里,量大时评分会被生成堵住
3. **FASHN 改 webhook 驱动** —— 现在轮询期间 worker 是阻塞的,
   一个 worker 同时只能盯一个任务。这是量上来后第一个撞到的瓶颈
4. **仪表盘加缓存** —— 十几条聚合查询,后台访问频率低时不值得,量大再说

## 九、安全清单

上线前逐项确认(需求第十九章,自动化部分见 `tests/pure/test_security_audit.py`):

- [ ] `.env` 不在版本库里,密钥用 secret 管理
- [ ] `POSTGRES_PASSWORD` 已改
- [ ] 数据库与 Redis 不对公网暴露
- [ ] `PUBLIC_BASE_URL` 是 HTTPS
- [ ] `DOWNLOAD_ALLOWED_HOSTS` 只填了确实需要的内网主机
- [ ] 浏览器登录三项已配(`ADMIN_PASSWORD` / `OPERATOR_PASSWORD` /
      `AUTH_SESSION_SECRET`),两个密码不同、密钥不是占位值
- [ ] 后台前端不直接暴露公网,或至少放在反向代理的认证之后
      (登录只有两个固定账号,**不是**完整账号体系 —— 见下)
- [ ] 上传大小上限 `MAX_UPLOAD_SIZE_MB` 符合实际需要
- [ ] 日志投递目标本身是受控的(日志里有商品信息)

### 关于账号体系:有登录,但没有用户表

**这一节以前写的是「MVP 没有账号体系 —— 这是当前最大的安全缺口」。a46 之后
那句话不再成立**:浏览器打开页面要先登录,两个固定账号 `admin` / `operator`,
密码由部署的人在 `.env` 里配,服务端发 HttpOnly 签名 Cookie。
未登录的匿名请求进不了任何业务接口。

**但它仍然不是完整的账号体系**,三条限制必须写进部署方案:

    没有用户表        账号写死两个,不能注册、不能自助改密。要按人追溯,
                      下一步是「用户表 + 每人一个账号」,不是去配 OPERATOR_TOKENS
                      的具名口令(那是机器凭据)
    审计追不到个人    五个人共用 `operator` 密码时,审计日志里全部记成 operator。
                      界面顶栏会把这件事**显式说出来**,不让人误以为自己被追踪到了
    退不了已发的 Cookie  签名 Session 是无状态的,服务端没有"哪些会话还有效"的表。
                      改密码不会让已登录的人掉线;要立刻全员失效,只能换
                      `AUTH_SESSION_SECRET` 并重启(那等于把所有人当场登出)

所以网络层那道防线仍然建议保留:把后台放在 VPN 或反向代理的认证之后。
只是它从"唯一的防线"降级成了"第二道"。

机器凭据(`ADMIN_TOKEN` / `OPERATOR_TOKENS`,给 CLI、脚本、pytest)和上面这套
浏览器登录是**两回事**,改一边不影响另一边 —— 见 README「浏览器登录」
与 `docs/DECISIONS.md` §3.66 / §3.68。

---

## 十、Windows 与 macOS 上的部署

前面各节假设 Linux。Windows 与 macOS 上**服务本身没有任何差异**(全部跑在容器里),
差异全在容器之外的三处:**换行符、宿主机网络、以及没有 `make`**。

这一节按「先做什么、会撞到什么」的顺序写,不是按平台分栏 —— 两个平台有一半的坑是共同的。

### 10.1 共同的三件事(不做会卡住)

#### ① 换行符:`.env` 带 `\r` 会让密码悄悄变错

仓库里没有 `.gitattributes`,而 Windows 上 `git clone` 默认 `core.autocrlf=true`
会把文本文件改写成 CRLF。后果最隐蔽的一处是 `.env`:

```
POSTGRES_PASSWORD=s3cret\r
```

`env_file` 把 `\r` 当成密码的一部分带进容器,于是 backend 连不上库,
而日志只说认证失败 —— 密码"看起来"完全正确。同理 CRLF 的 `Makefile` 会让
GNU make 报 `missing separator`。

**clone 之前**先设好,或者建一个 `.gitattributes`:

```bash
git config --global core.autocrlf input     # Windows 上推荐 input,不是 true
```

```gitattributes
# .gitattributes —— 建议直接加进仓库
* text=auto eol=lf
*.png binary
*.jpg binary
```

已经 clone 过的:`git config core.autocrlf input && git rm --cached -r . && git reset --hard`。
只想救 `.env` 的话,用编辑器另存为 LF 即可。

> macOS 不会自动改写换行,但如果 `.env` 是从 Windows 同事那里拷来的,同样中招。
> 排查一句话:`file .env` 输出里出现 `CRLF` 就是它。

#### ② 自建 ComfyUI 在宿主机上:必须显式放行 `host.docker.internal`

这是 Windows / macOS 上**最容易卡住的一条**,而且报错信息不会指向真正的原因。

容器里的 `localhost` 是容器自己,不是你的宿主机。Docker Desktop 提供
`host.docker.internal` 指回宿主机,但它解析到一个**私有地址**
(通常 `192.168.65.x`),而 `app/core/net_safety.py` 的规则是
**私有地址需要显式放行**(第九节的 SSRF 防线)。所以只填地址是不够的:

```bash
COMFYUI_BASE_URL=http://host.docker.internal:8188
DOWNLOAD_ALLOWED_HOSTS=host.docker.internal      # ← 这行不加就一定失败
```

不加的表现是 `RESULT_DOWNLOAD_FAILED` 或 `UnsafeDownloadURL`,读起来像 ComfyUI 挂了,
其实是自己的安全层挡住的 —— 它挡得对,只是你没告诉它这台主机可信。

**Linux 上不一样**:`host.docker.internal` 默认不存在,要在 compose 里补

```yaml
  worker:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

放行私有地址是有代价的:它给这套系统开了一条能访问你内网的路。**只填确实需要的主机名,
不要填网段**,并且线上如果 ComfyUI 有独立域名,就用域名而不是 `host.docker.internal`。

#### ③ 端口占用:5432 / 6379

compose 把这两个端口绑到 `127.0.0.1`(见 §3 与 `docker-compose.yml` 的注释)。
如果宿主机已经装了 PostgreSQL 或 Redis,`docker compose up` 会直接失败。

**容器之间走 compose 内部网络,本来就不需要发布这两个端口。** 最省事的做法是删掉:

```yaml
  postgres:
    # ports:
    #   - "127.0.0.1:5432:5432"
```

需要用图形客户端连库的话改成别的端口(`127.0.0.1:15432:5432`),
或者干脆走 `docker compose exec postgres psql -U imagegen -d imagegen`。

---

### 10.2 Windows

#### 环境

| 项 | 要求 |
| --- | --- |
| Docker Desktop | 用 **WSL2 后端**,不要用已废弃的 Hyper-V 后端 |
| WSL2 发行版 | Ubuntu 22.04 或更新;`wsl --update` 一次 |
| 商业使用 | Docker Desktop 对一定规模以上的企业需要付费订阅,自查许可;也可以只装 WSL2 + Docker Engine |

#### 仓库放在 WSL2 里,不要放在 `C:\`

```bash
# 在 WSL2 终端里
cd ~ && git clone <repo> && cd swimwear-imagegen
```

放在 `/mnt/c/Users/...` 下会有两个后果,都不报错、只是难受:

- **bind mount 慢一个量级** —— `./backend:/app` 每次 import 都跨文件系统边界;
  `npm install` 可能从 40 秒变成 10 分钟。
- **`--reload` 不生效** —— 跨 `/mnt/c` 的文件变更不产生 inotify 事件,改代码后
  uvicorn 不重启,你会以为改的东西没生效。真要放在 Windows 盘上,加一句:

  ```yaml
    backend:
      environment:
        WATCHFILES_FORCE_POLLING: "true"     # 轮询代替 inotify,费 CPU 但能用
  ```

#### 没有 `make`:每条目标的原生等价命令

Windows 上没有 `make`(也没有 `grep` / `awk`,所以 `make help` 也不能用)。
装一个也行(`winget install GnuWin32.Make`,或在 WSL2 里 `apt install make` —— 
**在 WSL2 终端里跑就等于 Linux,下表可以跳过**)。在 PowerShell 里直接用:

| `make` 目标 | PowerShell / CMD 等价命令 |
| --- | --- |
| `make up` | `docker compose up -d --build` |
| `make down` | `docker compose down` |
| `make logs` | `docker compose logs -f backend worker` |
| `make migrate` | `docker compose exec backend alembic upgrade head` |
| `make seed` | `docker compose exec backend python -m app.scripts.seed_sample_data` |
| `make test` | `docker compose exec backend pytest` |
| `make test-pure` | `cd backend; python tools\run_pure_tests.py` ← 注意是 `python` 不是 `python3` |
| `make smoke` | `docker compose exec backend python -m app.scripts.smoke_test` |
| `make worker-ping` | `docker compose exec backend python -c "from app.tasks.health_tasks import ping; print(ping.delay().get(timeout=10))"` |
| `make requeue APPLY=1` | `docker compose exec backend python -m app.scripts.requeue_stranded --apply` |
| `make calibrate` | `docker compose exec backend python -m app.scripts.calibrate` |
| `make psql` | `docker compose exec postgres psql -U imagegen -d imagegen` |
| `make clean` | `docker compose down -v` |

**`python3` 在 Windows 上不存在。** Python 官方安装包只提供 `python.exe`;
输入 `python3` 会被 Microsoft Store 的占位程序接走,弹出应用商店。
凡是文档里写 `python3` 的地方,在 Windows 上一律换成 `python`。

#### `make secret-key` 的替代

生成设置页主密钥(`SETTINGS_SECRET_KEY`)。Windows 没有 `openssl`:

```powershell
python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

纯 PowerShell 版本(连 Python 都不用):

```powershell
$b = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
[Convert]::ToBase64String($b).Replace('+','-').Replace('/','_')
```

#### 杀毒软件

Defender 实时扫描会把上传与出图拖慢(每张图落盘都被扫一遍),
`node_modules` 的安装也一样。把这几个目录加进排除项:
WSL2 发行版根目录、Docker Desktop 的数据目录、以及 `storage` 卷。
**只在开发机上这么做**,生产机不要为了性能关掉扫描。

---

### 10.3 macOS

#### Apple Silicon:不需要做任何事

四个基础镜像 `python:3.11-slim`、`postgres:16-alpine`、`redis:7-alpine`、
`node:22-alpine` **都有官方 arm64 版本**,`docker compose up` 直接原生跑,
不需要 `--platform linux/amd64`,也没有 QEMU 模拟的性能损失。

> 反过来说:**如果将来往 compose 里加了只有 amd64 的镜像**(某些自建 AI 推理镜像
> 就是这样),那个服务需要显式写 `platform: linux/amd64`,并且会慢 3–10 倍。
> 出现「容器起来了但 CPU 一直 100%」先怀疑这个。

#### 内存要给够

五个服务(postgres / redis / backend / worker / beat)加上 Pillow 出图,
Docker Desktop 默认的内存上限偏紧。**Settings → Resources → Memory 给到 6 GB 以上**;
`make seed` 生成示例图或批量出图时不够会表现为容器被静默 OOM kill,
日志里只看到 worker 突然消失。

```bash
docker stats            # 看实时占用
docker inspect <container> | grep -i oomkilled
```

#### 文件共享用 VirtioFS

Docker Desktop 4.6+ 的 **VirtioFS** 比旧的 gRPC-FUSE 快得多,
而 `./backend:/app` 与 `./frontend:/app` 都是 bind mount,直接受它影响。
Settings → General → 勾选 VirtioFS。

#### 前端建议跑在本机,不进容器

`frontend` 服务每次 `up` 都执行 `npm install`,在 macOS 的 bind mount 上很慢。
开发时更舒服的做法是让容器只跑后端那一套:

```bash
docker compose up -d postgres redis backend worker beat
cd frontend && npm install && npm run dev
```

前端仍然通过 `VITE_API_BASE_URL=http://localhost:8000/api` 访问后端 —— 
宿主机上的 `localhost:8000` 就是 compose 发布出来的那个端口,不需要改任何配置。

#### 大小写不敏感的文件系统

APFS 默认大小写不敏感。存储路径是 sha256 的小写十六进制,**不受影响**;
但如果你手工往 `storage/` 里放过文件,注意 `Foo.jpg` 与 `foo.jpg` 在 macOS 上是
同一个文件、在容器(ext4)里是两个 —— 备份来回拷会对不上。

---

### 10.4 不用 Docker,直接跑在 Windows / macOS 上

只推荐用于开发。需要本机的 PostgreSQL 16 与 Redis 7。

```bash
# macOS
brew install postgresql@16 redis && brew services start postgresql@16 && brew services start redis

# Windows:推荐在 WSL2 里 apt install,或用官方 PostgreSQL 安装包 +
# Memurai(Redis 的 Windows 移植);原生 Redis 已不再维护 Windows 版
```

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

两处平台差异:

- **`SETTINGS_KEY_DIR` 的权限。** 主密钥落在 `.secrets/`(默认),
  容器里靠卷隔离。本机跑的时候它就是一个普通目录,自己收权限:
  macOS `chmod 700 .secrets`;Windows 在文件属性里去掉 `Users` 组的读取权限。
  **别把它放进 `storage/`** —— 那个目录会被 `/files` 对外托管,启动时会复查这一点并拒绝。
- **`make test-pure` 不需要任何依赖**,两个平台都能直接跑,是验证「代码本身有没有被
  改坏」最快的方式:`cd backend && python tools/run_pure_tests.py`。

### 10.5 一张对照表

| 症状 | Windows | macOS |
| --- | --- | --- |
| 数据库认证失败,密码看起来是对的 | `.env` 是 CRLF —— `file .env` 确认 | 同(从 Windows 拷来的 `.env`) |
| 改代码后不重启 | 仓库在 `/mnt/c/`,inotify 不通;移进 WSL2 或开轮询 | 少见;检查 VirtioFS 是否开启 |
| `npm install` 极慢 | 仓库在 `/mnt/c/` | bind mount;建议前端跑本机 |
| worker 突然消失、无报错 | 检查 WSL2 内存上限(`.wslconfig`) | Docker Desktop 内存给到 6 GB+ |
| ComfyUI 连不上 / `UnsafeDownloadURL` | `DOWNLOAD_ALLOWED_HOSTS=host.docker.internal` 没加 | 同 |
| `docker compose up` 端口失败 | 本机装了 PostgreSQL/Redis;注掉 compose 里的 `ports` | 同 |
| `python3: command not found` | 用 `python` | `python3` 正常 |
| `make: command not found` | 用 §10.2 的等价命令表,或在 WSL2 里跑 | `make` 自带 |
| CPU 长期 100%,容器却"正常" | — | 某个镜像没有 arm64,在 QEMU 里跑 |
