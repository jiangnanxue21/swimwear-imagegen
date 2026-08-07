.PHONY: help init up down logs migrate revision seed test test-pure smoke baseline calibrate lint arch-check verify-delivery verify-sample-data verify-imports audit-anchors audit-guards audit-columns audit-doc-refs fe-install fe-dev fe-check fe-build fe-e2e check check-offline p0-gate pack requeue cleanup secret-key worker-ping psql clean

help:
# 字符类里带上数字:`p0-gate` 这类带编号的目标原来会被这条 grep 静默滤掉 ——
# 目标在、`make help` 里看不见,而看不见的入口等于没有。
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## 首次准备:生成 .env
	@test -f .env || cp .env.example .env
	@echo ".env 就绪"

up: init ## 启动全部服务
	docker compose up -d --build

down: ## 停止并移除容器
	docker compose down

logs: ## 跟踪后端日志
	docker compose logs -f backend worker

migrate: ## 执行数据库迁移
	docker compose exec backend alembic upgrade head

revision: ## 生成迁移脚本 make revision M="add xxx"
	docker compose exec backend alembic revision --autogenerate -m "$(M)"

seed: ## 导入 10 个示例商品与素材
	docker compose exec backend python -m app.scripts.seed_sample_data

test: ## 容器内运行全部 pytest
	docker compose exec backend pytest

test-pure: ## 无需任何三方依赖的纯逻辑测试(本机 python3 即可)
	cd backend && python3 tools/run_pure_tests.py

lint: ## Ruff
	cd backend && ruff check app tests

arch-check: ## 依赖方向契约(取代 test_import_graph.py),需已装后端依赖
	cd backend && lint-imports

verify-delivery: ## 交付卫生 + 门禁接线自检(取代 test_delivery_hygiene.py)
	cd backend && python3 tools/verify_delivery.py

verify-sample-data: ## 样例数据自检(取代 test_stage2_pipeline.py 里的 4 条)
	cd backend && python3 tools/verify_sample_data.py

# A29:`tests/test_poll_and_delist_db.py` 里有一条 `from app.models.listing_export
# import ListingExport`,那个模块全仓不存在。它躺在一个默认 skip 的数据库用例的
# **函数体**里 —— 本机永远绿,CI 上直接 error,而它从写下来那天起没被求值过一次。
# 顶层 import 一 collect 就会炸,函数体内的要等那一行真的被执行,所以这一类
# 只能靠静态解析在跑之前抓住。零依赖、秒级,放在离线子集里。
verify-imports: ## app.* 的 import 是否都指向真实存在的东西(零依赖)
	cd backend && python3 tools/verify_imports.py

# A45-batch14-2 走读:变异脚本的锚点会过期,而**失锚的变异不报错、不变红,
# 只是安静地什么都没验**。变异脚本自己发现不了 —— 它跑一次要几十份工作树、
# 十几分钟,所以刻意不进 CI(见 `mutate_batch14.py` 顶部),于是锚点可以
# 过期很久没人知道(F3 一次让 M33/M34 同时失锚)。
#
# 这一条只解析、不执行:`mutate_contract_tests.py` 的变异循环写在模块顶层,
# 没有 `__main__` 保护 —— import 它就是当场改写真实工作树。审计工具把被审
# 对象跑起来,本身就是事故。`ast` 只要求语法合法,秒级、零子进程,进离线子集。
audit-anchors: ## 变异脚本的锚点还对不对(只解析,不执行)
	cd backend && python3 tools/audit_anchors.py

audit-doc-refs: ## 文档与注释里的路径引用指不指得到东西(活文档拦,历史台账只提示)
	cd backend && python3 tools/audit_doc_refs.py

audit-columns: ## 每一列都答得出「谁写它」(落库无写入路径的列会被点名)
	cd backend && python3 tools/audit_column_writers.py

audit-guards: ## 守卫的窗口封不封闭(反向断言不许吃切窄的源码)
	cd backend && python3 tools/audit_source_guards.py

# R1-36:开发用 `npm ci` 而不是 `npm install`。
# 两者装出来的树可以不一样(`install` 会按 semver 悄悄升次版本并改写 lockfile),
# 于是「本地好的、CI 红的」这类问题没有任何线索。生产镜像一直是 `npm ci`,
# 开发这一侧对齐之后,lockfile 才真的是一份契约。
# 要故意升依赖时显式跑 `npm install <pkg>` 并提交 lockfile。
fe-install:
	cd frontend && npm ci

fe-dev:
	cd frontend && npm run dev

# ---------------------------------------------------------------- 前端门禁(评审 P1-1)
#
# 在此之前 `npm run typecheck` / `lint` / `build` 三条**没有任何流程会调用**,
# `tools/syntax-check.mjs` 连 npm script 都没有 —— 只有 docs/STATUS.md 里
# 一行手敲命令。而 `docs/STATUS.md` 自己记着这件事从 A12 就发现了。
#
# 这四项现在由 .github/workflows/ci.yml 的 frontend job 执行,顺序与此处一致。
# make 目标仍然要全包 —— 本地跑得到的门禁范围一旦比 CI 窄,
# 分叉的方向永远是本地更松,而人是在本地决定"这版可以推了"的。
#
# 顺序是有讲究的:typecheck 最快、报错最准,先跑它;
# build 最慢,但它是唯一能验证产物真的能生成的一步,放最后。
fe-check: ## 前端全部门禁:装依赖 + 类型 + lint + 测试 + 构建(需联网)
	cd frontend && npm ci
	cd frontend && npm run typecheck
	cd frontend && npm run lint
	cd frontend && npm run test
	cd frontend && npm run build
	cd frontend && npm run syntax-check

fe-e2e: ## Playwright 主流程(需先 npx playwright install chromium)
	cd frontend && npm run e2e

fe-build: ## 只构建前端产物到 frontend/dist
	cd frontend && npm ci && npm run build

# ---------------------------------------------------------------- 门禁(v4.1 Phase 0)
#
# `check` 现在**覆盖全部门禁**,包括前端那四条。
#
# 方案 4.1 节 G-0 写得很直接:没有 CI 之前,清单写多长都没有意义;
# 而退而求其次的最低要求是「把 fe-check 并入 make check,让一条命令覆盖全部门禁」。
# 现在 CI 有了(.github/workflows/ci.yml),但 `make check` 仍然要能一条命令跑全 ——
# 否则本地与 CI 的门禁范围会分叉,而分叉的方向永远是本地更松。
#
# 想要旧的那个离线子集,用 `check-offline`,它会明说自己漏了什么。

check: check-offline fe-check ## 全部门禁(需联网:前端要装依赖)
	@echo
	@echo "全部门禁通过 —— 后端 + 架构 + 交付 + 前端类型/lint/测试/构建。"

# ---------------------------------------------------------------- 离线子集(A45 batch12-5 收尾)
#
# ## 要修的那件事
#
# 原来 `check-offline` 把 `lint` 和 `arch-check` 当**前置目标**:
#
#     check-offline: test-pure lint arch-check verify-delivery ...
#
# make 的前置目标一个失败,后面全部不跑。于是在装不上 pip 工具的环境里
# (batch12-5 评审的环境正是如此),整条门禁**停在 ruff 缺失**那一步 ——
# 而排在它后面的 verify-delivery / verify-sample-data / verify-imports
# 三条零依赖检查根本没有机会执行,评审只能一条条手跑。
#
# 「离线子集」的意义就是把离线环境里**能跑的全跑了**;一个可选工具的缺失
# 让它汇报得比实际能做到的少,这本身就是缺陷。
#
# ## 修法:工具缺失 -> 大声跳过;工具在 -> 照旧严格
#
# 写法上有一个必须绕开的坑:`command -v ruff && ruff check || echo SKIP`
# 会把 **ruff 真实的检查失败**也吞进 `||` 分支 —— 工具在、代码有问题,
# 门禁却绿了。所以只能用 if/else:检测和执行分开,执行失败照常让 make 中止。
#
# `lint` / `arch-check` 两个独立目标保持原样严格:CI 和装好了工具的本机
# 用它们时,缺工具就该炸。宽松的只有 check-offline 这一个入口,
# 而它的每一次宽松都会打印出来。
#
# 顺带把前端语法体检收进来:`tools/syntax-check.mjs` 只要 node,
# 不要 node_modules(它自己的文档第一句就是这个用途),此前却只有
# `fe-check`(需联网装依赖)会跑它 —— 离线环境里它明明跑得动。
check-offline: test-pure verify-delivery verify-sample-data verify-imports audit-anchors audit-guards audit-doc-refs ## 离线子集(不需要 node_modules;缺 pip 工具时大声跳过)
	@if command -v ruff >/dev/null 2>&1; then \
		cd backend && ruff check app tests; \
	else \
		echo "SKIP  lint(ruff 未安装)—— 这一项没有被验证"; \
	fi
	@if command -v lint-imports >/dev/null 2>&1; then \
		cd backend && lint-imports; \
	else \
		echo "SKIP  arch-check(lint-imports 未安装)—— 这一项没有被验证"; \
	fi
	@if command -v node >/dev/null 2>&1; then \
		cd frontend && node tools/syntax-check.mjs; \
	else \
		echo "SKIP  前端语法体检(node 未安装)—— 这一项没有被验证"; \
	fi
	@echo
	@echo "离线子集跑完。上方每一行 SKIP 都是一项**没有被验证**的门禁;"
	@echo "它**不包含**前端类型、lint、Vitest 与构建 —— 这四条只有 \`make check\`"
	@echo "(或 CI)才会跑到。不要拿这一条当作'全都过了'。"

# ---------------------------------------------------------------- 阶段 P0 清单
#
# PRD v3.1.1 §14.3:「开始真实人工测试前必须满足:阶段 P0 全部关闭」。
# 在此之前「关没关闭」的唯一依据是 docs/STATUS.md 里的几句话,而它们过期
# 不会有任何东西报错。这条命令把那份清单变成可执行的。
#
# **它不在 CI 里,是刻意的**(理由写在 tools/p0_gate.py 顶部):它跨越 CI 的
# job 边界(库在 backend、依赖在 frontend、镜像在 images),放进任何一个 job
# 都会因为别的 job 才有的前提而永远报「未验证」—— 一条恒红的门禁会被人加
# `|| true`。它的读者是准备冻结人工测试版本的那个人,不是每一次提交。
p0-gate: ## 阶段 P0 清单:逐条跑,跑不动的点名说缺什么(退出码非零 = 未关闭)
	cd backend && python3 tools/p0_gate.py $(ARGS)

pack: ## 打交付包 make pack V=a20
	tools/pack.sh $(V)

smoke: ## 生成链路冒烟:健康→素材→已授权模特→生成→评分→成品图→导出(不含审核后链路)
	docker compose exec backend python -m app.scripts.smoke_test

calibrate: ## 评分器校准:人工判定 vs 模型分档的一致率
	docker compose exec backend python -m app.scripts.calibrate

baseline: ## Provider 基线对比 make baseline SKU=SW-001-BLK-S P=mock,fashn
	docker compose exec backend python -m app.scripts.provider_baseline --sku $(SKU) --providers $(or $(P),mock)

requeue: ## 找出并重新派发滞留任务 make requeue APPLY=1
	docker compose exec backend python -m app.scripts.requeue_stranded $(if $(APPLY),--apply,)

# 4.1 节 H 的清理预案。默认只看不做 —— 加 APPLY=1 才真的排队下架。
# CMD 默认 inventory:最无害的那个子命令,手滑跑成什么都不会改。
cleanup: ## 人工测试清理 make cleanup TAG=uat-1 CMD=inventory|verify|delist APPLY=1
	docker compose exec backend python -m app.scripts.cleanup_test_listings \
		$(or $(CMD),inventory) $(if $(TAG),--tag $(TAG),) $(if $(SHOP),--shop $(SHOP),) \
		$(if $(APPLY),--apply,)

secret-key: ## 生成一把设置页主密钥,填进 .env 的 SETTINGS_SECRET_KEY
	@python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

worker-ping: ## 验证 Celery worker 存活
	docker compose exec backend python -c "from app.tasks.health_tasks import ping; print(ping.delay().get(timeout=10))"

psql: ## 进 psql(跟随 POSTGRES_USER / POSTGRES_DB,不写死)
# A-42:原来是 `psql -U imagegen -d imagegen` 写死。改过库名或用户名的部署
# 跑这一条会连不上,而报错是 psql 的"role does not exist" —— 看起来像库坏了。
# 默认值与 docker-compose.yml 里那两处 `${POSTGRES_USER:-imagegen}` 一致。
	docker compose exec postgres psql -U $${POSTGRES_USER:-imagegen} -d $${POSTGRES_DB:-imagegen}

# ---------------------------------------------------------------- 运维脚本
#
# 两条都**默认干跑**,加 --apply 才真的改。它们处理的是评审 B-07 / C-17 那两批
# 数据 —— 前者要人判断归属,后者不可逆,都不适合做成定时任务。

purge-exports: ## 回收被作废的批量导出对象(C-17,默认干跑)
	cd backend && python3 -m tools.purge_exports $(ARGS)

repair-variant-owners: ## 列出/归属裸 VARIANT owner(B-07,默认干跑)
	cd backend && python3 -m tools.repair_variant_owners $(ARGS)

clean:
	docker compose down -v
	rm -rf backend/storage backend/.pytest_cache
