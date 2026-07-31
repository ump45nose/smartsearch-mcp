# SmartSearch Remote MCP

一个把 [SmartSearch](https://github.com/konbakuyomu/smartsearch) 变成远程
MCP 服务的轻量网关。它通过 Streamable HTTP 暴露只读搜索能力，使用
FastMCP 的 OAuth 2.1/OIDC Proxy 接入 Authelia，让 ChatGPT、Codex、Hermes
和其他兼容 MCP 的客户端可以安全地共享同一套检索能力。

## 这个项目解决什么问题？

本项目不是另一个搜索引擎，而是一个“安全的远程入口”：

- 把搜索、抓取、站点地图、路由和研究统一成 MCP 工具；
- 先完成 OAuth、PKCE、consent 和用户白名单校验，再允许调用；
- 保留 Hermes 原有的 stdio SmartSearch，远程服务独立部署、独立回滚；
- 对公网抓取做 SSRF 防护、参数边界、超时、并发和限流；
- 研究结果写入按 UUID 隔离的 evidence 目录，客户端只拿到摘要和
  `artifact_id`，不暴露宿主机路径。

## 五个工具

| 工具 | 用途 | 适合什么时候用 |
| --- | --- | --- |
| `smart_route` | 判断一个问题需要哪些检索能力，不实际访问远程网页 | 先规划检索方案 |
| `smart_search` | 搜索当前信息并返回来源、provider 和耗时 | 查新闻、资料、产品信息 |
| `smart_fetch` | 抓取一个公开 URL 并提取正文 | 已经有目标页面时 |
| `smart_map` | 先了解一个站点的页面结构 | 再决定要抓哪些页面时 |
| `smart_research` | 发现来源、抓取页面、检查缺口并整理证据 | 需要可追溯的研究结论时 |

所有工具都声明为只读；服务端不会对目标网站写入内容。`smart_doctor` 仅供
容器内运维探针使用，不会出现在 MCP 客户端的工具列表中。

## 当前部署入口

公网客户端使用：

~~~text
https://smartsearch-mcp-home.172906573.xyz:28443/mcp
~~~

NAS 局域网客户端使用同一个域名但不带端口：

~~~text
https://smartsearch-mcp-home.172906573.xyz/mcp
~~~

公网必须保留 `:28443`。NAS 上的 Codex/Hermes 使用无端口地址，OAuth 回调
再分别转回各自的临时监听器；远程电脑不要套用 NAS 的回调覆盖参数。

## 快速部署（NAS）

在本仓库根目录执行。根 `.env` 只作为 Compose 插值来源；只有
`compose.yaml` 显式列出的变量会进入容器，切勿 source 任意 Hermes Profile
的 `.env`。

~~~bash
docker compose --env-file /home/hermes/.hermes/.env build
docker compose --env-file /home/hermes/.hermes/.env up -d
docker compose --env-file /home/hermes/.hermes/.env ps
~~~

容器默认不发布宿主端口，只加入外部 `gateway_net`，以非 root 用户运行，根文件
系统只读，并启用 `cap_drop: ALL`、`no-new-privileges` 和资源上限。持久化数据位于：

~~~text
/vol2/1000/Docker/smartsearch-mcp/data
~~~

OAuth 初次启用、Authelia 配置、Nginx Proxy Manager、DNS/LAN 分流和回滚步骤见
[OPERATIONS.md](OPERATIONS.md)。

## 客户端接入

### NAS 上的 Codex

第一次添加远程 MCP 时，使用 LAN URL，并把 OAuth 回调指定到 NAS 的临时监听器：

~~~bash
codex mcp add \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote \
  --url https://smartsearch-mcp-home.172906573.xyz/mcp
~~~

如果已添加过，只需重新登录：

~~~bash
codex mcp login \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote
~~~

保持命令运行，在浏览器完成 Authelia 登录、2FA 和 consent。完成后确认：

~~~bash
codex mcp list
codex mcp get smartsearch-remote --json
~~~

输出应显示 HTTP URL 和 OAuth，而不是 `command`/`stdio`。

### NAS 上的 Hermes

保留已有的 stdio `smart-search`，另外添加远程服务：

~~~yaml
mcp_servers:
  smartsearch-remote:
    url: "https://smartsearch-mcp-home.172906573.xyz/mcp"
    auth: oauth
    connect_timeout: 315
    timeout: 900
    enabled: true
    oauth:
      redirect_port: 5556
      redirect_uri: "https://smartsearch-mcp-home.172906573.xyz:28443/hermes-oauth-callback/lingjun"
~~~

授权时临时启动回环桥，再执行登录；授权完成后停止桥：

~~~bash
python3 scripts/hermes_oauth_loopback_bridge.py \
  >/tmp/smartsearch-hermes-oauth-bridge.log 2>&1 &
bridge_pid=$!
trap 'kill "$bridge_pid" 2>/dev/null || true' EXIT INT TERM

sudo -n -u hermes \
  /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp login smartsearch-remote
~~~

登录完成后，用下面两步验证并让运行中的 Gateway 重新加载凭据：

~~~bash
sudo -n -u hermes /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp test smartsearch-remote
sudo -n -u hermes /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp warm smartsearch-remote
~~~

### ChatGPT Developer Mode

在 ChatGPT 的 **Settings → Apps → Create** 中填写：

| 字段 | 值 |
| --- | --- |
| Name | `SmartSearch Home` |
| Endpoint | `https://smartsearch-mcp-home.172906573.xyz:28443/mcp` |
| Authentication | `OAuth` |
| Registration method | `Dynamic Client Registration (DCR)` |
| Token endpoint auth | `none` |

完成 OAuth 后，工具扫描结果应严格为上面的五个工具。不要选择 CIMD；当前部署
使用 DCR。创建完成后，请在新对话中真实调用一次 `smart_route`、`smart_fetch`
或 `smart_search`，仅“健康检查成功”不能证明客户端侧调用可用。

### 远程电脑上的 Codex

远程电脑使用公网地址，让 Codex 回到这台电脑自己的 localhost：

~~~powershell
codex mcp add smartsearch-home --url https://smartsearch-mcp-home.172906573.xyz:28443/mcp
codex mcp login smartsearch-home
~~~

不要把 NAS 专用的 `mcp_oauth_callback_url` 复制到远程电脑。

## 安全边界和运行特性

- OAuth authorization-code、S256 PKCE、refresh token、DCR、consent 和 2FA；
- 只允许配置的 Authelia 用户访问；
- `smart_fetch`/`smart_map` 拒绝内网、回环和非公开目标，防止 SSRF；
- 单用户工具限流 30 次/分钟，研究限流 4 次/15 分钟；全局并发 2，研究并发 1；
- 输入长度、URL、深度、广度、超时和输出大小都有上限，超长结果会被脱敏并截断；
- 研究 evidence 按 UUID 隔离并保留 30 天，服务启动和后台任务会清理过期数据；
- 审计日志只保留工具名、匿名主体、耗时、状态、provider 数量和 artifact ID，
  不写入查询、网页正文、OAuth token 或 provider key。

## 固定版本和开发检查

当前镜像固定 Python `3.11.13`、FastMCP `3.4.4` 和 SmartSearch
`0.1.14-beta.8`（commit `667c465d0f6ea16a423f03c434f94e21505d3595`）。所有
Python 依赖及 hash 都记录在 `requirements.lock`，Dockerfile 提供 `test` 阶段。

~~~bash
docker build --target test -t local/smartsearch-mcp:test .
docker compose --env-file /home/hermes/.hermes/.env config --quiet
docker compose --env-file /home/hermes/.hermes/.env build
~~~

运行中的最小探针：

~~~bash
docker exec smartsearch-mcp python /app/scripts/doctor_probe.py
docker exec smartsearch-mcp python /app/scripts/smoke_probe.py
~~~

探针只输出状态、provider 数量、来源 URL、artifact ID 等必要字段，不输出查询、
正文或凭据。验收时请分开记录四个状态：服务健康、OAuth 已授权、客户端发现五个
工具、真实工具调用成功。

## 故障排查和回滚

先看容器和最近日志：

~~~bash
docker compose --env-file /home/hermes/.hermes/.env ps
docker logs --since 10m smartsearch-mcp
~~~

- `Scan Tools` 无法连接：确认 ChatGPT 使用带 `:28443/mcp` 的公网 URL；
- OAuth 提示 client 未注册：选择 DCR，不要选择 CIMD；
- OAuth 成功但看不到工具：新建对话并显式选择带 `Dev` 标记的 App；
- 真实调用失败：先跑 `doctor_probe.py` 和 `smoke_probe.py`，再检查 provider 状态。

完整回滚顺序见 [OPERATIONS.md](OPERATIONS.md)：删除精确的 NPM 代理、停止
Compose（不要加 `-v`）、恢复已验证的 Authelia 配置，并保留 data 目录。回滚远程
服务不会修改 Hermes 原有的 stdio SmartSearch。

## 文档和许可证

部署、授权、接入、验证、升级和回滚的用户流程必须同步维护在本 README；更细的
内部 runbook 放在 [OPERATIONS.md](OPERATIONS.md)。本仓库当前没有单独的 `LICENSE`
文件；再分发前请先确认上游 SmartSearch 及本仓库的授权边界。
