# FrontierAgent 内网部署

## 适用范围

本文面向无公网出口的内网环境。内网模式下，除配置的内部端点外，运行时不得向任何外部地址发起连接。阅读前请先确认两点：

- 内网 LLM 网关地址与密钥（`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` / `DEEPSEEK_BASE_URL` 等，配置于 `.env`）。
- 内网搜索引擎的协议形态（Serper 兼容或 Anthropic Messages 兼容，见下文「搜索接入」）。

`deploy/huggingface/`（HF Space 发布）与 `benchmarks/`（HF 数据集与 judge 端点）不在本文范围，内网环境不应运行。

## 网络策略

网络策略由 `frontier_agent/infra/network_policy.py` 统一执行，默认 fail-closed：

- `FRONTIER_AGENT_INTRANET_ONLY` 默认 `1`。内网模式下，出站目标必须解析为私有网段（RFC1918、回环、链路本地、IPv6 ULA）；解析到公网地址的端点会被拒绝，请求不会发出。
- `FRONTIER_AGENT_ALLOWED_NETWORK_CIDRS` 追加企业网段，`FRONTIER_AGENT_ALLOWED_NETWORK_HOSTS` 追加信任主机名。主机名放行后仍会解析校验，公网地址不会因主机名列表而被放行。
- `FRONTIER_AGENT_TELEMETRY` 默认 `off`。仓库内无第三方遥测 SDK，usage/trace 记录全部本地落盘到 `<workspace>/.apodex/runs/<id>/`（含 `trace.jsonl`），不产生网络请求。`workflows/_shared/sdk_shim.py` 的对外协议钩子均为 no-op，运行时无人给 `metadata` 的 `sdk_*` 键接线。
- 两个语义不可互换：网络失败保持 fail-open（搜索失败返回空结果，agent 不崩溃）；安全与权限策略保持 fail-closed。

内网模式同时禁用以下路径（返回明确错误，不静默）：

| 路径 | 行为 |
|---|---|
| `web_fetch` / `web_fetch_aligned` | 返回 `[BLOCKED]`，彻底关闭 |
| `download_file` | 返回 blocked，彻底关闭 |
| E2B 云沙箱 | `sandbox_backend=e2b` 被拒绝 |
| 沙箱命令（bwrap） | `allow_net=False`，无网络命名空间 |
| 无网络隔离的沙箱后端 | 拒绝执行，报 `SandboxUnavailable` |
| OCR / Vision | 默认离线；`FRONTIER_AGENT_ALLOW_READDOC_REMOTE=1` 且端点私有才恢复 |
| 剪贴板 broker | 默认 macOS 原生读取，不设 `APODEX_CLIPBOARD_BROKER_URL` |

## 搜索接入

搜索是内网模式下唯一的网络检索路径，有两条实现，由 profile 开关选择：

| 实现 | 文件 | 配置方式 |
|---|---|---|
| original（默认） | `plugins/tools/web_search.py` | config 类（`get_config()`），读 env |
| aligned | `plugins/tools/web_search_aligned.py` | 调用时直接读 env `SERPER_BASE_URL` / `SERPER_API_KEY` |

original 的搜索链顺序：有 `SERPER_API_KEY` 走 Serper；否则 `ANTHROPIC_SEARCH_ENABLED=true` 且有 key 时走 anthropic 通道；再否则走 deepseek 通道（`DEEPSEEK_SEARCH_ENABLED`，复用 `DEEPSEEK_API_KEY`）；再否则返回空结果。aligned 只走 `SERPER_BASE_URL` / `SERPER_API_KEY`，默认空，内网必须显式配置。

内网搜索引擎按协议接入：

- Serper 兼容：`.env` 设 `SERPER_BASE_URL=http://<内网>:<port>` 与 `SERPER_API_KEY`，两条实现均覆盖。
- Anthropic Messages 兼容（`web_search_20250305` server tool）：设 `ANTHROPIC_SEARCH_ENABLED=true`、`ANTHROPIC_SEARCH_BASE_URL`、`ANTHROPIC_SEARCH_API_KEY`，original 路径即生效。aligned 如需支持需另加通道。
- 自有 REST：照 `web_search.py` 的 `_deepseek_search` 写法新增通道，把结果映射成 Serper 形状的 `organic[]` 后喂给既有 `_format_results`。

内网搜索引擎当前协议形态「待确认」。若内网没有正文抓取服务（Jina 替代），`web_fetch` 保持关闭；是否需要直连抓取策略「待确认」。

## LLM 端点

`config/providers.yaml` 各 provider 的默认值解析自环境变量。内网部署把 `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` / `DEEPSEEK_BASE_URL` 指向内网 LLM 网关即可，不必改动 profile。Docker 镜像路径按客户拉取 `ghcr.io/apodexai/frontieragent`，内网环境改为 native 运行或预载镜像。

## 验证

- 测试：`uv run pytest tests/ apodex/tests/`。
- 出网证明（三选一或组合）：
  1. 内网端点配置下跑 `apodex -p "一个查询"`（native 模式），读 run record 的 usage 计数器，看 serper / jina / deepseek / search 各计数。该计数器兼作验证仪器，可证明调了谁、没调谁。
  2. `HTTPS_PROXY` 指向一个日志代理再跑，抓全部出网请求。
  3. 运行期间用 `lsof -i` 观察建立的连接。
- 降级验证：内网端点不可达时，搜索返回空结果，agent 不崩溃（fail-open 语义保持）。

## 实施清单

- [ ] `.env` 配置内网 LLM 网关与内网搜索端点（真实值只进本地 `.env`，不进仓库）。
- [ ] `.env.example` 已含全部内网键（占位值）与注释。
- [ ] `FRONTIER_AGENT_INTRANET_ONLY` 保持默认（不设或设 `1`）。
- [ ] 无 `api.deepseek.com` / `google.serper.dev` / `r.jina.ai` 出网（以 usage 计数器或代理日志为准）。
- [ ] 全量测试通过。