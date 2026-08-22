# 后端镜像。两阶段:编译期的东西留在 builder,最终镜像只带运行需要的。
#
# ## 这个文件从前有一个从未被验证过的假设
#
# 原来的顺序是:
#
#     COPY pyproject.toml ./
#     RUN pip install -e ".[dev]"      # ← 此刻 app/ 还不存在
#     COPY . .                         # ← 源码在这一步才进来
#
# `pyproject.toml` 声明 `[tool.setuptools.packages.find] include = ["app*"]`,
# 而执行 `pip install -e` 时构建上下文里只有 `pyproject.toml` —— setuptools
# 扫到的包集合是空的。能不能 `import app` 取决于它为这个布局挑了哪种 editable
# 策略(`_StaticPth` 把项目根加进 sys.path 则可用,`_TopLevelFinder` 按安装期
# 快照建映射则不可用),**两种都可能,而没有任何证据表明是哪一种**。
#
# 开发态一直看不出来:`docker-compose.yml` 把 `./backend:/app` 挂进容器,
# 宿主源码盖住镜像内容。而 `docker-compose.prod.yml` 用 `!override` 移掉了
# 那个挂载 —— 生产完全依赖镜像里的东西。开发绿、生产未知,中间那道缝没人跨过。
#
# 现在的处理是把不确定性整个拿掉:装成**非 editable**,并且源码在安装之前就位。
# 依赖层的缓存靠拆两步保住(见下),不靠 editable。

# ---------------------------------------------------------------- 构建
FROM python:3.11-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src

# 编译器与 libpq 头文件只在这一阶段需要。它们不进最终镜像 ——
# 一个带 gcc 的生产容器,拿到执行权的人手里就多了一整套工具链。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# 第一步:只装**依赖**。
#
# 此刻 app/ 确实还不存在,而这一次的目的正是如此 —— setuptools 扫不到包,
# 产出一份只有元数据的安装,但 `dependencies` 里那一串照样被解析并装进 venv。
# 这一层只随 `pyproject.toml` 失效,改一行业务代码不必重装依赖。
#
# 不装 `[dev]`:pytest / ruff / import-linter 属于 CI 的测试 job,
# 把它们烧进生产镜像换不来任何东西,只是体积与攻击面。
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# 第二步:源码就位之后**真正把 app 装进去**。`--no-deps` 让上面那一层的
# 缓存继续有效 —— 依赖已经在 venv 里了,这一步只补包本身。
COPY . .
RUN pip install --no-deps --force-reinstall .

# ---------------------------------------------------------------- 运行
FROM python:3.11-slim

# `PYTHONPATH=/app` 让 `/app` 排在 site-packages 之前,两种形态都要照顾:
# 开发态 `./backend:/app` 是 bind mount(改一行立刻生效,这是 `--reload`
# 存在的前提),生产态 `/app` 是镜像里 COPY 进来的那一份。少了这一行,
# 开发态跑的会是 venv 里那份**安装时的快照**,而宿主上改的代码一行都不生效 ——
# 而且不报错,只是行为对不上,那是最难查的一类。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app

WORKDIR /app

# 运行期只留两样:libpq5(psycopg 的运行时依赖)与 curl(健康检查要用它,
# compose 的 healthcheck 也是 curl)。**没有编译器。**
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
COPY . .

# 构建期自检:**装完之后 `import app` 到底成不成立**,在这里就要有答案。
#
# 这和前端 Dockerfile 里的 `nginx -t` 是同一个思路 —— 把「起来之后才发现」
# 变成「构建失败」。放在运行阶段而不是构建阶段:这里验的是**最终镜像里的
# 那一份**,构建阶段验过不代表 COPY --from 之后还成立。
#
# 两遍不是重复:
#
#     第一遍  带 PYTHONPATH   验的是**运行时真正会被 import 的那一份**(/app)
#     第二遍  去掉 PYTHONPATH  且切到 /(免得 `python -c` 把 CWD 加进 sys.path)——
#             验的是 venv 里那份安装。上面那个 editable 空包集的坑,只有这一遍看得见
#
# `APP_ENV` 默认 local,所以浏览器登录三项不配也能 import(`_check_browser_auth`
# 只在非 local 环境拦);`create_engine` 不连库,`storage_dir.mkdir` 在 lifespan
# 里 —— 这一句不需要任何外部服务。
RUN python -c "import app, app.main; print('import ok: /app')" \
    && (cd / && env -u PYTHONPATH python -c "import app, app.main; print('import ok: venv')")

# 非 root 运行。
#
# `/data/storage` 与 `/data/secrets` 在这里先建出来并交给 app —— docker 创建
# **新的**命名卷时会用镜像里该路径的内容与属主去初始化它,所以这两行决定了
# 卷的属主。主密钥文件要 0600 写入,存储目录要落成品图,两个都写不进去的话
# 表现是「后台一个 API Key 都存不下」和「出图落库成功但文件不见了」。
#
# **升级既有部署要留意**:已经存在的卷是过去以 root 建的,属主不会因为换了
# 镜像而改变。升级时需要一次
#
#     docker compose run --rm --user root backend chown -R app:app /data
#
# 否则容器起来之后写不进去,而现象是权限错误散在各条业务路径上,不指向卷。
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/storage /data/secrets \
    && chown -R app:app /data /app

USER app

EXPOSE 8000

# 打 `/health` 而不是 `/health/ready`。
#
# 就绪探针答的是「依赖齐不齐」,它挂了不该让编排重启容器 —— 数据库抖一下
# 就滚动重启一轮后端,是把一次短暂降级放大成一次不可用。存活探针只问
# 「这个进程还在不在」。
#
# 前缀跟着 API_PREFIX 走,不写死 /api:写死之后改这个配置的人会发现健康检查
# 永远失败,而现象是「容器一直 unhealthy」,完全不指向前缀。
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS "http://localhost:8000${API_PREFIX:-/api}/health" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
