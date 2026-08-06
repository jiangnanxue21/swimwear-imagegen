# 前端生产镜像(评审 P0-3)。
#
# 在此之前这套前端**只以开发服务器的形态存在过**:compose 里跑的是
# `npm run dev`,Makefile 里没有 build 目标,全仓没有任何地方调用 `npm run build`。
# 后果分两层 —— 运行时(开发服务器不压缩、不做内容哈希、把源码和 sourcemap
# 一起发出去,而它同时是唯一对外发布的服务),以及**验证层**:
# `npm run build` 会跑 `tsc -b`,而 tsc 是唯一能发现类型错误的东西。
# 构建从没跑过,等于类型正确性至今零证据。
#
# 这个文件的第一阶段就是那道验证 —— 类型错了镜像直接构建失败,
# 而不是等到打开页面才发现。

# ---------------------------------------------------------------- 构建
FROM node:22-alpine AS build
WORKDIR /app

# 先只拷两份清单:依赖没变时这一层命中缓存,改一行业务代码不必重装。
# 用 `npm ci` 而不是 `npm install` —— 前者严格按 lockfile 装,
# 两份清单不一致时**直接失败**而不是悄悄把 lockfile 改掉。
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# `npm run build` = `tsc -b && vite build`。类型错误在这里就会中断构建。
RUN npm run build

# ---------------------------------------------------------------- 运行
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
# A45-#7:安全头 snippet。漏掉这一行的表现是 nginx 起不来
# (`include` 找不到文件是启动期错误),所以它不会静默失效
COPY nginx-security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html

# 配置语法自检(A45-BATCH8 合入)。写错一个分号的表现本来是容器起来了、
# 健康检查一直不过,要翻容器日志才知道是配置问题;而 `nginx -t` 会把它变成
# 一次**构建失败**,带确切文件名和行号。
#
# 放在两条 COPY 之后:它检查的正是这两份文件合起来的结果 ——
# snippet 里写错了,只有主配置 `include` 到它时才会暴露。
RUN nginx -t

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -qO- http://localhost/healthz || exit 1
