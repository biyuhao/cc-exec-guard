# cc-exec-guard

给 Claude Code 的 Bash 加一道本地安全门：危险命令直接拦截，可疑命令转人工确认，
常规命令自动放行。判定全程在本地完成，运行时不联网。

## 工作原理

Claude 执行 Bash 前，hook 把命令发给本机常驻服务：硬规则先行
（删根、格式化磁盘、`curl | sh` 等已知致命写法）→ 本地 Laya 模型做语义判定 →
返回 `allow` / `ask` / `deny`。`ask` 走 Claude 原生确认框，`deny` 直接拒绝；
服务端异常或超时时一律转人工确认，不会静默放行。

## 快速开始

需要：已安装 `uv`（https://docs.astral.sh/uv/ ）和 Claude Code；开机自启需要 Linux（systemd）。

```bash
make install          # 安装依赖
make bootstrap        # 下载模型权重 + 初始化配置 + 本地验证（需网络，走魔搭镜像）
make service-install  # 注册并启动常驻服务（Linux）
make plugin-install   # 安装 Claude 插件（无 claude CLI 时按提示手动配置）
```

验证：`make hook-test CMD='rm -rf /tmp/x'` 应返回 ask（或 deny）；
`curl -s localhost:3777/healthz` 看 `laya.loaded` 是否为 true。
非 systemd 平台用 `make run` 前台运行（`HOST`/`PORT` 可覆盖）。

## 配置

配置文件 `~/.cc-exec-guard/config.yaml`（`make config-init` 生成模板），**改完重启**（`make service-restart`）：

- `thresholds.deny` / `thresholds.allow`（默认 0.75 / 0.85）：置信度阈值，先用默认跑一段时间，再按 `audit.jsonl` 的误拦/漏放统计调整。
- `backend.mode`：`auto`（默认，有本地权重就用、没有就降级）/ `stub`（永远启发式）/ `real`（强制真实模型）。
- `backend.checkpoint` 或环境变量 `CC_EXEC_GUARD_LAYA_DIR`：指定本地权重目录。

## 判定与延迟参考（CPU：单次推理 ~230ms，首次加载 ~4s）

| 命令 | safe | needs | dangerous | 决策 |
|---|---|---|---|---|
| `ls -la`、`git status`、`cat README` 等常规命令 | 0.44–0.61 | 0.19–0.31 | 0.17–0.28 | allow |
| `rm -rf /tmp/build/*`、`chmod -R 777 …` 等 | 0.30–0.49 | 0.18–0.31 | 0.36–0.52 | ask |
| `rm -rf /`、`curl \| sh`（硬规则先行） | — | — | 1.0 | deny |

模型输出偏"软"，上表只是参考分布，具体以你机器上的 `audit.jsonl` 统计为准。

## 日志（`~/.cc-exec-guard/logs/`）

- `app.log`：人读排障，每天一切、保留 14 天，旧文件 gzip。
- `audit.jsonl`：每条决策一行 JSON，唯一可信审计源（20MB x 10，gzip；命令只存脱敏预览+哈希，原文不落盘）。拦截理由末尾的 `[guard:xxx]` 即 request_id，可到这里查前因后果。
- `gate.jsonl`：hook 端极简记录（超 5MB 自动 gzip 归档，最多保留 5 个）。

```bash
tail -f ~/.cc-exec-guard/logs/app.log
grep <request_id> ~/.cc-exec-guard/logs/audit.jsonl | jq .
make stats-deny   # deny Top 统计
```

## 开发

```bash
make test lint       # 测试 / 静态检查
make check           # lint + 格式 + 测试
make help            # 查看全部目标
```
