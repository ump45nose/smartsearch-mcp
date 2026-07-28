# SmartSearch 远程 OAuth MCP

这是 [SmartSearch](https://github.com/konbakuyomu/smartsearch) 的独立远程
MCP 服务。它使用 Streamable HTTP 暴露工具，并通过 FastMCP `OIDCProxy` 与
Authelia 提供 OAuth 2.1 鉴权。

Hermes 原有的 stdio SmartSearch 不会被替换或修改；远程服务是单独的
Docker Compose 栈，可供 ChatGPT、Codex 和兼容的 Claude 客户端使用。

公网 MCP 地址：

```text
https://smartsearch-mcp-home.172906573.xyz:28443/mcp
```

公网必须显式使用 `28443`，因为运营商封锁了 443，网关将公网 TCP `28443`
转发到 Nginx Proxy Manager 的 HTTPS 端口。

## 对外工具

- `smart_search`
- `smart_fetch`
- `smart_map`
- `smart_route`
- `smart_research`

`smart_doctor` 仅供容器内运维探针使用，不对 MCP 客户端开放。

服务额外提供：

- OAuth authorization-code、PKCE、refresh token、DCR 和 consent
- 只允许指定 Authelia 用户访问
- fetch/map 的 SSRF 防护
- 按用户限流、全局并发 2、research 并发 1
- 参数边界、脱敏、输出截断和 15 分钟请求上限
- UUID research artifact 与 30 天 evidence 保留
- 不记录查询、网页正文、OAuth token 或 provider key 的最小化审计日志

## 固定版本

- Python `3.11.13`
- FastMCP `3.4.4`
- SmartSearch `v0.1.14-beta.8`
- SmartSearch commit `667c465d0f6ea16a423f03c434f94e21505d3595`

稳定版 `v0.1.14` 没有五个工具所需的完整 route/research API，因此这里固定到
可复现的 beta commit。

## 构建和测试

所有 Python 依赖及传递依赖都在 `requirements.lock` 中按 hash 锁定。

```bash
docker build --target test -t local/smartsearch-mcp:test .
docker compose --env-file /home/hermes/.hermes/.env config --quiet
docker compose --env-file /home/hermes/.hermes/.env build
```

Compose 服务不发布宿主端口，只加入外部 `gateway_net`。容器使用
`1000:1001`、只读根文件系统、`cap_drop: ALL` 和
`no-new-privileges`。

仓库不保存 OAuth、provider、DNS 或 TLS 密钥。部署仅把 `compose.yaml`
显式列出的根 `.env` 变量传入容器，禁止 source 任意 Hermes Profile `.env`。

完整部署、反代、回滚说明见 [OPERATIONS.md](OPERATIONS.md)。下面的中文流程覆盖
日常客户端接入和授权。

## 先判断自己属于哪种接入

| 场景 | MCP 地址 | OAuth 回调 |
|---|---|---|
| NAS 上的 Codex | 不带端口的 LAN 地址 | 公网回调路径转回 NAS `5555` |
| NAS 上的 Hermes | 不带端口的 LAN 地址 | 公网回调路径经临时桥转到 `127.0.0.1:5556` |
| Windows/其他远程电脑上的 Codex | 带 `:28443` 的公网地址 | 浏览器回到该电脑自己的 localhost |

不要把 NAS 的公网回调覆盖参数复制到远程电脑。两者监听位置不同。

## NAS 本机 Codex：配置并生成授权链接

NAS 使用 LAN 分流后的 443 地址：

```text
https://smartsearch-mcp-home.172906573.xyz/mcp
```

### 1. 检查是否已有同名配置

```bash
codex mcp get smartsearch-remote --json
```

如果命令提示不存在，直接执行下一步。如果存在，必须确认输出中是 `url`，并且
URL 等于上面的 LAN 地址。出现 `command` 或 `stdio` 表示同名项类型错误。

### 2. 首次添加并启动 NAS 的 headless OAuth

当前 Codex 在 `mcp add --url` 后会自动探测 OAuth，并可能立即打印授权链接。
首次添加时应直接带上 NAS 公网回调覆盖，避免自动生成一个只能在 NAS 本机打开的
localhost 回调：

```bash
codex mcp add \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote \
  --url https://smartsearch-mcp-home.172906573.xyz/mcp
```

如果该命令已经打印授权链接，就直接按下面的流程完成授权，不需要同时再开一条
`mcp login`。

### 3. 已配置后的重新授权

```bash
codex mcp login \
  -c mcp_oauth_callback_port=5555 \
  -c 'mcp_oauth_callback_url="https://smartsearch-mcp-home.172906573.xyz:28443/codex-oauth-callback"' \
  smartsearch-remote
```

流程：

1. 保持该命令运行，不要关闭终端。
2. 命令会打印一条很长的 `https://smartsearch-mcp-home.../authorize?...` 链接。
3. 把链接发给能打开浏览器的人；浏览器不必在 NAS 上。
4. 在 Authelia 完成登录、2FA 和 consent，只点一次“允许”。
5. 浏览器经公网 `:28443/codex-oauth-callback/...` 回到 NAS 的临时 `5555`
   监听器。
6. 终端出现 `Successfully logged in` 后，临时监听器自动结束。

验证：

```bash
codex mcp list
codex mcp get smartsearch-remote --json
```

列表中应显示 HTTP URL，Auth 应为 OAuth。

## NAS 本机 Hermes：配置并生成授权链接

保留已有 stdio `smart-search`，另加一个 `smartsearch-remote`：

```yaml
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
```

Hermes 只监听 `127.0.0.1:5556`，因此授权期间需要临时桥接
`192.168.31.201:5556 -> 127.0.0.1:5556`。

在 NAS 上运行：

```bash
python3 /vol2/1000/Docker/stacks/smartsearch-mcp/scripts/hermes_oauth_loopback_bridge.py \
  >/tmp/smartsearch-hermes-oauth-bridge.log 2>&1 &
bridge_pid=$!
trap 'kill "$bridge_pid" 2>/dev/null || true' EXIT INT TERM

sudo -n -u hermes \
  /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp login smartsearch-remote
```

流程：

1. 保持 Hermes login 命令运行。
2. 复制终端打印的授权链接，在其他电脑的浏览器打开。
3. 完成 Authelia 登录、2FA 和 consent，只点一次“允许”。
4. 浏览器经公网 Hermes callback 回到 NAS，再由临时桥转发给 Hermes。
5. 看到 `Authenticated — 5 tool(s) available` 后关闭临时桥。

验证并让运行中的 Gateway 加载凭据：

```bash
sudo -n -u hermes \
  /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp test smartsearch-remote

sudo -n -u hermes \
  /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun mcp warm smartsearch-remote

sudo -n -u hermes \
  /home/hermes/.hermes/hermes-agent/venv/bin/hermes \
  --profile lingjun gateway restart
```

`mcp test` 应显示连接成功并发现 5 个工具。

## ChatGPT 网页端：Developer Mode 接入

ChatGPT 只连接远程 MCP，必须使用带 `:28443` 的公网地址。当前 ChatGPT Pro
在 Developer Mode 下只保证读取类自定义 MCP；本服务的五个工具都声明为
read-only。

### 1. 创建开发版 App

在 ChatGPT 网页端进入：

```text
Settings → Apps → Create
```

不同语言或灰度版本可能显示为“设置 → 应用 → 创建”或“创建自定义连接器”。

建议填写：

| 字段 | 值 |
|---|---|
| Name | `SmartSearch Home` |
| Description | `私有只读网页搜索、抓取、站点地图、路由与研究工具` |
| MCP Server URL / Endpoint | `https://smartsearch-mcp-home.172906573.xyz:28443/mcp` |
| Authentication | `OAuth` |
| Advanced OAuth → Registration method | `动态客户端注册（DCR）` |
| Token endpoint authentication method | `none` |

服务支持 DCR，因此不要手工填写 OAuth Client ID、Client Secret 或 provider
token，也不要把 Authelia 的 client secret 复制到 ChatGPT。

当前部署不要选择 CIMD。FastMCP 容器的 SSRF-safe CIMD fetch 对 ChatGPT
`client.json` 的 TLS 握手失败，服务端会把 CIMD client ID 判定为未注册客户端；
DCR 已通过公网 registration endpoint 的真实测试。

### 2. 创建/扫描工具并完成 OAuth

1. 点击 `Create`；部分界面版本会先显示 `Scan Tools`。
2. ChatGPT 应跳转到 Authelia；完成登录、2FA 和 consent，只点一次“允许”。
3. 回调地址应为
   `https://chatgpt.com/connector/oauth/<callback_id>`，不要改写或删掉
   callback ID。
4. 回到 ChatGPT，等待工具扫描结束。

扫描结果应严格为五个工具：

- `smart_search`
- `smart_fetch`
- `smart_map`
- `smart_route`
- `smart_research`

如果少于或多于五个，先不要点击 `Create`，保存报错或截图并检查服务端日志。
五个工具正确后点击 `Create`。

### 3. 在新对话验收

创建一个新对话，从工具/Apps 菜单选择带 `Dev` 标记的 `SmartSearch Home`。
依次测试：

```text
请只使用 SmartSearch Home 的 smart_route，分析“OpenAI MCP OAuth 2.1”需要哪些检索能力。
```

```text
请只使用 SmartSearch Home 的 smart_fetch，抓取
https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
并返回页面标题。
```

```text
请只使用 SmartSearch Home 的 smart_search，搜索“OpenAI MCP OAuth 2.1”，
返回至少一个引用链接。
```

验收时分别记录：

- App 已创建
- OAuth 已完成
- 扫描到五个工具
- `smart_route`/`smart_fetch` 真实调用成功
- `smart_search`/`smart_research` 是否被 Pro 产品权限或 provider 状态限制

健康检查或成功扫描不能代替真实工具调用。

### ChatGPT 常见问题

- `Scan Tools` 无法连接：确认 URL 带 `:28443/mcp`，不能使用 NAS 的无端口地址。
- OAuth 提示 client ID 未注册：高级 OAuth 设置必须选择
  `动态客户端注册（DCR）`，不要选择 CIMD。
- OAuth 跳转丢失 `:28443`：不要继续授权，保存当前完整 URL 后检查 Authelia
  反代 rewrite。
- `redirect_uri` 被拒绝：确认回调主机是 `chatgpt.com`，路径是
  `/connector/oauth/<callback_id>`。
- OAuth 完成但扫描卡住：检查 `docker logs --since 10m smartsearch-mcp`，
  日志中不得包含 token 或查询正文。
- App 创建成功但对话里看不到：新建对话，在工具/Apps 菜单显式选择
  `SmartSearch Home`，并确认它带 `Dev` 标记。

## Windows 或其他远程电脑：公网接入

建议使用独立名称 `smartsearch-home`，避免与电脑上已有 stdio
SmartSearch 或其他配置撞名。

首次接入时运行：

```powershell
codex mcp add smartsearch-home --url https://smartsearch-mcp-home.172906573.xyz:28443/mcp
```

当前 Codex 会在添加 HTTP MCP 后自动探测 OAuth，并立即打开或打印授权链接。
保持 PowerShell 窗口运行，在浏览器完成授权，浏览器会回到这台远程电脑自己的
`127.0.0.1:<随机端口>`。

如果 MCP 已经添加过、只是要重新授权，再运行：

```powershell
codex mcp login smartsearch-home
```

授权完成后验证：

```powershell
codex mcp list
codex mcp get smartsearch-home --json
```

### 报错：OAuth login is only supported for streamable HTTP servers

含义：当前名称对应的是 stdio MCP，不是用 `--url` 添加的 Streamable HTTP MCP。
OAuth 只支持 HTTP MCP。

先检查出错名称：

```powershell
codex mcp get smartsearch-remote --json
```

推荐做法是不删除旧配置，直接使用上面的新名称：

```powershell
codex mcp add smartsearch-home --url https://smartsearch-mcp-home.172906573.xyz:28443/mcp
```

`mcp add` 会直接生成/打开 OAuth 链接。只有以后重新授权时才需要
`codex mcp login smartsearch-home`。

如果确认旧的 `smartsearch-remote` 配置不再需要，也可以删除后重建：

```powershell
codex mcp remove smartsearch-remote
codex mcp add smartsearch-remote --url https://smartsearch-mcp-home.172906573.xyz:28443/mcp
codex mcp login smartsearch-remote
```

不要在远程电脑上设置 NAS 专用的
`mcp_oauth_callback_url=https://.../codex-oauth-callback`；远程电脑应使用
Codex 默认 localhost 回调。

## 常用验证

查看服务与容器：

```bash
docker compose --env-file /home/hermes/.hermes/.env ps
docker logs --since 10m smartsearch-mcp
```

容器内配置与 provider 连通性探针：

```bash
docker exec smartsearch-mcp smart-search doctor
```

健康、OAuth metadata、401、PKCE、五工具 schema、SSRF、限流、输出截断和
30 天清理的自动化测试位于仓库测试套件。客户端接入完成后仍需分别验证：

- MCP 配置类型确实是 Streamable HTTP
- OAuth 登录完成
- 能发现 5 个工具
- 至少真实调用一次工具

“已配置”“已授权”“发现工具”“工具真实调用成功”是四个不同状态，不能互相代替。

## 数据和日志

每次 research 使用独立 UUID 目录，evidence 在 30 天后清理。审计日志只记录
工具名、匿名主体、耗时、状态、provider 数量和 artifact ID，不记录查询、
网页正文、OAuth token 或 provider key。

## 回滚

完整回滚见 [OPERATIONS.md](OPERATIONS.md)。客户端侧通常只需：

```bash
codex mcp logout <名称>
codex mcp remove <名称>
```

停止远程 Compose 栈不会修改 Hermes 原有 stdio SmartSearch。

## 文档维护约定

以后凡是新增或修改部署、授权、接入、验证、故障排查、升级或回滚流程，都必须在
同一个变更中同步更新本 README 的中文操作说明；不能只修改脚本、配置或
`OPERATIONS.md` 而遗漏 README。
