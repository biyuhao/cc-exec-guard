# cc-exec-guard 常用操作封装：优先用 `make <target>`，细节见 `make help`
# 说明：
#   - 所有变量均可用 `make run HOST=0.0.0.0 PORT=3778` 方式覆盖
#   - hook 端只用标准库，`hook-test` 直接用系统 python3 即可
#   - systemd 模板 `systemd/cc-exec-guard.service` 里的旧路径已过时，
#     `service-install` 会按当前目录动态生成 unit，不直接拷贝

DEFAULT_GOAL := help

UV      ?= uv
HOST    ?= 127.0.0.1
PORT    ?= 3777
APP     := cc_exec_guard.server:app
GUARD_URL := http://$(HOST):$(PORT)

CONFIG  ?= $(HOME)/.cc-exec-guard/config.yaml
LOG_DIR ?= $(HOME)/.cc-exec-guard/logs

HOOK         := plugins/claude/scripts/gate.py
PLUGIN_JSON  := plugins/claude/.claude-plugin/plugin.json
MARKETPLACE_JSON := plugins/.claude-plugin/marketplace.json
HOOKS_JSON   := plugins/claude/hooks/hooks.json

SERVICE_UNIT := cc-exec-guard.service
SYSTEMD_SRC  := systemd/cc-exec-guard.service
SYSTEMD_DST  := $(HOME)/.config/systemd/user/$(SERVICE_UNIT)

# hook 烟囱测试默认命令：make hook-test CMD='rm -rf /tmp/x'
CMD ?= echo hello-cc-exec-guard

# 真实权重（默认走魔搭 ModelScope 镜像）：
#   make model-download [MODEL_ID=owner/name]  -> .models/laya
#   HF 镜像站可用时也可设 HF_ENDPOINT 再走 huggingface-cli
MODEL_ID ?= convaiinnovations/laya
MS_HOME  := $(CURDIR)/.ms-home
MS_CACHE := $(CURDIR)/.ms-cache
CHECKPOINT_DIR ?= $(CURDIR)/.models/laya

.PHONY: help install lock run dev health metrics test lint lint-fix format format-check check \
	config-init hook-test plugin-check plugin-install \
	service-install service-uninstall service-restart service-status service-logs \
	logs-app logs-audit logs-gate stats-deny add-laya model-download model-warmup bootstrap reinstall clean

help: ## 显示本帮助（默认目标）
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## 首次同步环境（生成 .venv + 装 dev 依赖）
	$(UV) sync --group dev

reinstall: ## 重建 .venv（搬目录/换 Python 后修复 shebang 里的绝对路径）
	rm -rf .venv
	$(UV) sync --group dev

lock: ## 刷新 uv.lock
	$(UV) lock

run: ## 前台运行 cc-exec-guard（HOST/PORT 可覆盖）
	$(UV) run uvicorn $(APP) --host $(HOST) --port $(PORT)

dev: ## 本地开发运行（带 --reload）
	$(UV) run uvicorn $(APP) --host $(HOST) --port $(PORT) --reload

health: ## 检查 cc-exec-guard 存活（需先 make run）
	curl -fsS $(GUARD_URL)/healthz; echo

metrics: ## 查看 allow/ask/deny 计数器
	curl -fsS $(GUARD_URL)/metrics; echo

test: ## 跑测试（可追加 TEST_ARGS='-k xxx -q'）
	$(UV) run pytest -q $(TEST_ARGS)

lint: ## ruff 静态检查
	$(UV) run ruff check .

lint-fix: ## ruff 自动修可修项
	$(UV) run ruff check --fix .

format: ## ruff 格式化
	$(UV) run ruff format .

format-check: ## 只检查格式，不改文件
	$(UV) run ruff format --check .

check: lint format-check test ## 一键全检：lint + 格式 + 测试

config-init: ## 初始化 ~/.cc-exec-guard/config.yaml（已存在则不动）
	mkdir -p $(HOME)/.cc-exec-guard $(LOG_DIR)
	@if [ ! -f $(CONFIG) ]; then cp config.example.yaml $(CONFIG); echo "created $(CONFIG)"; else echo "keep $(CONFIG)"; fi

hook-test: ## 模拟 Claude 调 gate.py（需 cc-exec-guard 在线，CMD='...' 覆盖命令）
	printf '{"tool_input":{"command":%s},"session_id":"make-test","cwd":"/tmp"}' "$$(printf '%s' '$(CMD)' | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
		| CC_EXEC_GUARD_URL=$(GUARD_URL)/v1/classify python3 $(HOOK)

plugin-check: ## 校验 marketplace/plugin JSON（有 claude CLI 时用官方 validate）
	python3 -m json.tool $(MARKETPLACE_JSON) > /dev/null && echo "ok $(MARKETPLACE_JSON)"
	python3 -m json.tool $(PLUGIN_JSON) > /dev/null && echo "ok $(PLUGIN_JSON)"
	python3 -m json.tool $(HOOKS_JSON) > /dev/null && echo "ok $(HOOKS_JSON)"
	@if command -v claude >/dev/null 2>&1; then \
		claude plugin validate $(CURDIR)/plugins && claude plugin validate $(CURDIR)/plugins/claude; \
	fi
	python3 -m py_compile $(HOOK) && echo "ok $(HOOK) compile"
	chmod +x $(HOOK) && echo "ok $(HOOK) executable"

plugin-install: plugin-check ## 安装本地插件（有 claude CLI 走 marketplace，否则打印手动配置）
	@if command -v claude >/dev/null 2>&1; then \
		claude plugin marketplace add $(CURDIR)/plugins && claude plugin install cc-exec-guard@cc-exec-guard; \
	else \
		echo "未找到 claude CLI，请手动把钩子写入 ~/.claude/settings.json："; \
		echo ""; \
		echo '{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "$(CURDIR)/$(HOOK)", "timeout": 5 } ] } ] } }'; \
	fi

service-install: config-init model-download ## 生成 unit、备齐权重、写入 checkpoint 并启动（装完即真实可用）
	mkdir -p $(HOME)/.config/systemd/user
	@if [ ! -f $(CHECKPOINT_DIR)/model.safetensors ]; then echo "权重仍缺失：$(CHECKPOINT_DIR)/model.safetensors 不存在"; exit 1; fi
	@if grep -qE '^[[:space:]]*checkpoint:[[:space:]]*""' $(CONFIG); then \
		sed -i 's|^[[:space:]]*checkpoint:.*|  checkpoint: "$(CHECKPOINT_DIR)"|' $(CONFIG); \
		echo "wired backend.checkpoint -> $(CHECKPOINT_DIR)"; \
	else \
		echo "keep existing backend.checkpoint in $(CONFIG)"; \
	fi
	sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$(CURDIR)|" \
	    -e "s|^ExecStart=.*|ExecStart=$(CURDIR)/.venv/bin/uvicorn $(APP) --host $(HOST) --port $(PORT)|" \
	    -e "s|^Environment=.*|Environment=CC_EXEC_GUARD_CONFIG=$(CONFIG)|" \
	    $(SYSTEMD_SRC) > $(SYSTEMD_DST)
	systemctl --user daemon-reload
	systemctl --user enable --now $(SERVICE_UNIT)
	systemctl --user restart $(SERVICE_UNIT)
	systemctl --user status $(SERVICE_UNIT) --no-pager

service-uninstall: ## 停掉并移除 user 级 systemd unit
	-systemctl --user disable --now $(SERVICE_UNIT)
	rm -f $(SYSTEMD_DST)
	-systemctl --user daemon-reload

service-restart: ## 重启 cc-exec-guard（改完 config.yaml 后用）
	systemctl --user restart $(SERVICE_UNIT)
	systemctl --user status $(SERVICE_UNIT) --no-pager

service-status: ## 查看 cc-exec-guard 服务状态
	systemctl --user status $(SERVICE_UNIT) --no-pager

service-logs: ## 看 journald 实时日志
	journalctl --user -u $(SERVICE_UNIT) -f

logs-app: ## tail app.log（人读排障）
	mkdir -p $(LOG_DIR)
	@if [ ! -f $(LOG_DIR)/app.log ]; then echo "hint: app.log 尚不存在，已建空文件占位；cc-exec-guard 处理第一条请求后开始写入"; touch $(LOG_DIR)/app.log; fi
	tail -F $(LOG_DIR)/app.log

logs-audit: ## tail audit.jsonl（机读审计）
	mkdir -p $(LOG_DIR)
	@if [ ! -f $(LOG_DIR)/audit.jsonl ]; then echo "hint: audit.jsonl 尚不存在，已建空文件占位；cc-exec-guard 处理第一条请求后开始写入"; touch $(LOG_DIR)/audit.jsonl; fi
	tail -F $(LOG_DIR)/audit.jsonl

logs-gate: ## tail gate.jsonl（hook 端极简日志）
	mkdir -p $(LOG_DIR)
	@if [ ! -f $(LOG_DIR)/gate.jsonl ]; then echo "hint: gate.jsonl 尚不存在，已建空文件占位；hook 触发一次后开始写入（去 Claude 里跑个 Bash，或 make hook-test）"; touch $(LOG_DIR)/gate.jsonl; fi
	tail -F $(LOG_DIR)/gate.jsonl

stats-deny: ## 统计今天 deny Top（只看 hash+reason，不暴露密钥）
	jq -r 'select(.decision=="deny") | [.command_hash[0:10], .reason] | @tsv' \
		$(LOG_DIR)/audit.jsonl | sort | uniq -c | sort -rn | head

add-laya: ## 加 Laya 官方包及推理依赖（laya/torch/transformers）
	$(UV) add "laya>=0.3.9" "torch>=2.3" "transformers>=4.44"

model-download: ## 从魔搭镜像拉权重到 .models/laya（只取 english 所需文件）
	MODELSCOPE_HOME=$(MS_HOME) MODELSCOPE_CACHE=$(MS_CACHE) \
	$(UV) run --with modelscope modelscope download $(MODEL_ID) \
		--local_dir $(CHECKPOINT_DIR) --cache-dir $(MS_CACHE) \
		--include "rl_agent_config.json" "model.safetensors" "tokenizer/*" "encoder/*"

model-warmup: ## 用本地权重强制加载验证（不联网，目录可 CHECKPOINT_DIR= 覆盖）
	CC_EXEC_GUARD_LAYA_MODE=real CC_EXEC_GUARD_LAYA_DIR=$(CHECKPOINT_DIR) $(UV) run python -c \
		"from cc_exec_guard.backends.laya import LayaBackend; b=LayaBackend(); \
		print('warmup:', b.warmup(download=False), b.status())"

bootstrap: install model-download model-warmup config-init ## 全新机器一条龙：环境 + 权重 + 验证 + 配置

clean: ## 清理本地缓存（不删 .venv / .uv-cache）
	rm -rf .pytest_cache .ruff_cache
	find . -name "__pycache__" -not -path "./.venv/*" -not -path "./.uv-cache/*" -exec rm -rf {} + 2>/dev/null; true
