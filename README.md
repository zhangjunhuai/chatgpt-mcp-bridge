# ChatGPT 电脑助手桥接服务

这个项目让 ChatGPT 在你的授权范围内操作一台 Linux 电脑。连接成功后，你可以直接在对话里让 ChatGPT 读取和修改文件、运行程序、下载资料，或查看长任务的进度。

例如，你可以这样说：

- “找出这个项目中所有包含 TODO 的 Python 文件。”
- “运行分析脚本，完成后告诉我结果。”
- “把这个链接里的数据下载到工作目录。”
- “修改配置文件，但不要碰其他目录。”

它适合个人电脑、家用服务器和实验工作站。项目目前面向 Linux。

## 工作方式

```text
ChatGPT → 安全隧道 → 本机 MCP 服务 → 允许访问的文件和程序
```

服务只监听 `127.0.0.1:8000`，不会直接向公网开放端口。ChatGPT 通过隧道访问它，每个连接地址还带有一段随机 Token。

你可以控制三件事：

- ChatGPT 能读取哪些目录
- ChatGPT 能写入哪些目录
- ChatGPT 能运行哪些程序

## 开始之前

你需要：

- Linux
- Python 3.10 或更高版本
- OpenAI Platform 中可用的 Secure MCP Tunnel
- ChatGPT 中的 Developer mode 和自定义应用权限

安装 Python 依赖：

```bash
python3 -m pip install "mcp==2.2.0"
```

## 快速启动

先创建专用工作目录和随机 Token：

```bash
mkdir -p "$HOME/chatgpt-workspace" "$HOME/chatgpt-downloads"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > token.txt
chmod 600 token.txt
```

然后启动服务：

```bash
python3 server_v2.2.py \
  --token-file token.txt \
  --readable "$HOME/chatgpt-workspace,$HOME/chatgpt-downloads,/tmp/chatgpt-mcp-jobs" \
  --writable "$HOME/chatgpt-workspace,$HOME/chatgpt-downloads,/tmp/chatgpt-mcp-jobs" \
  --default-workdir "$HOME/chatgpt-workspace" \
  --download-dir "$HOME/chatgpt-downloads" \
  --deny "$HOME/.ssh,$HOME/.gnupg,$HOME/.aws,$HOME/.config/gh,/etc,/boot,/root,/run"
```

启动后，终端会显示本地 MCP 地址：

```text
http://127.0.0.1:8000/mcp/<你的 Token>
```

仓库中的 `start.sh` 是当前部署机器使用的快捷脚本，里面包含固定的服务名、路径和隧道地址。换到其他电脑时，请先按自己的环境修改它。

## 准备 OpenAI Secure MCP Tunnel

ChatGPT 不能直接连接你电脑上的 `127.0.0.1`。OpenAI Secure MCP Tunnel 在电脑上运行一个 `tunnel-client`，由它主动向 OpenAI 发起 HTTPS 连接，并把收到的 MCP 请求转给本机服务。路由如下：

```text
ChatGPT
  ↓
OpenAI 托管的隧道端点
  ↓ 由 tunnel-client 通过出站 HTTPS 轮询
本机 http://127.0.0.1:8000/mcp/<Token>
```

你的电脑无需开放入站端口，也不需要公网 IP 或域名。运行 `tunnel-client` 的电脑需要满足两项网络条件：

- 能通过 HTTPS 访问 `api.openai.com:443`
- 能访问本机 MCP 地址 `http://127.0.0.1:8000/mcp/<Token>`

### 1. 确认账号和权限

隧道权限与 ChatGPT Developer mode 是两套权限：

- 在 OpenAI Platform 创建隧道需要 **Tunnels Read + Manage**。
- 运行客户端、在 ChatGPT 中选择隧道需要 **Tunnels Read + Use**。
- ChatGPT 工作区管理员还需要允许你使用 Developer mode。

功能是否可见取决于账号套餐和工作区策略。如果设置中没有 Tunnels 或 Developer mode，请联系 Platform 组织管理员或 ChatGPT 工作区管理员。

### 2. 在 OpenAI Platform 创建隧道

打开 [OpenAI Platform 的 Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)：

1. 创建一个 Tunnel，并记下形如 `tunnel_xxx` 的 `tunnel_id`。
2. 把 Tunnel 关联到准备使用它的 ChatGPT 工作区。
3. 创建供 `tunnel-client` 使用的 Runtime API key。
4. 从页面提供的下载入口获取最新版 `tunnel-client`。

只关联 Platform 组织还不够。没有关联目标 ChatGPT 工作区时，ChatGPT 的隧道列表中不会显示它。

### 3. 保存 Runtime API key

Runtime API key 属于敏感凭据。不要把它写进 README、启动脚本或 Git。可以把它保存到仅当前用户可读的文件：

```bash
mkdir -p "$HOME/.config/tunnel-client"
read -rsp "Runtime API key: " TUNNEL_API_KEY && echo
printf '%s\n' "$TUNNEL_API_KEY" > "$HOME/.config/tunnel-client/api-key"
chmod 600 "$HOME/.config/tunnel-client/api-key"
unset TUNNEL_API_KEY
```

### 4. 配置并检查 tunnel-client

先确保上一节启动的 MCP 服务仍在运行，再执行：

```bash
export CONTROL_PLANE_API_KEY="$(cat "$HOME/.config/tunnel-client/api-key")"

tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile chatgpt-bridge \
  --tunnel-id tunnel_xxx \
  --mcp-server-url "http://127.0.0.1:8000/mcp/$(cat token.txt)"

tunnel-client doctor --profile chatgpt-bridge --explain
tunnel-client run --profile chatgpt-bridge
```

将 `tunnel_xxx` 换成自己的 `tunnel_id`。下载到本地的程序如果没有放进 `PATH`，请把命令中的 `tunnel-client` 换成它的实际路径，例如 `./tunnel-client`。

`doctor` 应显示本机 MCP 服务可达、凭据有效且隧道已就绪。保持 `tunnel-client run` 运行；客户端停止后，ChatGPT 将无法发现或调用工具。

### 5. 在 ChatGPT 中添加

1. 打开 ChatGPT 的 **Settings → Security and login**，启用 **Developer mode**。
2. 打开 ChatGPT Plugins，点击加号创建开发者模式应用。
3. 填写名称和说明。
4. 在 **Connection** 中选择 **Tunnel**。
5. 从列表选择刚创建的 Tunnel，或填入它的 `tunnel_id`。
6. 如果界面要求选择认证方式，选择 **No authentication**。本项目用本机 MCP 路径中的随机 Token 限制访问，没有实现 OAuth。
7. 创建应用，检查 ChatGPT 是否发现了 13 个工具。

使用 Secure MCP Tunnel 时，不要把 OpenAI 托管的隧道端点填进 `server_url`，也不需要在这里填写 `127.0.0.1`。ChatGPT 只绑定 `tunnel_id`，本机地址由 `tunnel-client` 配置。

详细要求和最新界面请参考 [OpenAI Secure MCP Tunnel 官方文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 与 [ChatGPT 连接测试指南](https://developers.openai.com/plugins/deploy/connect-chatgpt)。

## ChatGPT 可以做什么

项目提供 13 个工具：

| 类别 | 能力 |
|---|---|
| 命令 | 运行短命令或 Python 代码 |
| 长任务 | 启动后台任务、查看进度、结束任务 |
| 文件 | 读取、创建、修改和补丁更新文本文件 |
| 查找 | 浏览目录、搜索文字、按规则查找文件 |
| 下载 | 把不超过 500 MB 的 HTTP(S) 文件保存到工作区 |

短命令最长运行 5 分钟。训练、仿真等耗时工作应使用后台任务工具，ChatGPT 可以持续查询它的状态、日志、CPU、内存和 GPU 显存占用。

## 安全边界

这项服务能执行命令和修改文件。启用前，请检查启动参数，使用单独的工作目录，并只开放必要权限。

| 设置 | 作用 |
|---|---|
| `--readable` | 允许读取的目录，多个目录用逗号分隔 |
| `--writable` | 允许写入的目录，多个目录用逗号分隔 |
| `--deny` | 始终禁止访问的目录，优先级最高 |
| `--executables` | 允许运行的程序名白名单 |
| `--default-workdir` | 未指定目录时，命令默认在哪里运行 |
| `--download-dir` | 下载文件的默认保存位置 |

命令以参数数组执行，不经过 shell 解析，因此 `;`、`&&` 和 `|` 不会变成额外命令。文件命令涉及的路径也会经过目录检查。

这些限制主要用于防止误操作。只要白名单中包含 `python3` 或 `bash`，获准调用工具的一方仍能运行代码。请像保护密码一样保护 `token.txt`，不要把 Token 发到公开聊天、截图或 Git 仓库中。服务以当前用户身份运行，不提供 `sudo`。

## 自检与排障

使用公网 HTTPS 地址或 Tailscale Funnel 时，可以测试握手、工具列表和命令执行：

```bash
./test.sh "https://你的公网地址/mcp/<你的 Token>"
```

`test.sh` 接收公网 URL，不接收 `tunnel_id`。使用 OpenAI Secure MCP Tunnel 时，请运行 `tunnel-client doctor --profile chatgpt-bridge --explain`，然后在 ChatGPT 创建应用时检查工具发现结果。

常见问题：

- **连接不上：** 确认 MCP 服务和隧道都在运行，并检查公网地址中是否包含正确的 Token。
- **ChatGPT 找不到 Tunnel：** 检查 Tunnel 是否关联了目标 ChatGPT 工作区，并确认账号拥有 Tunnels Read + Use 权限。
- **Tunnel 在线但工具发现失败：** 检查 `--mcp-server-url` 是否包含 `/mcp/<Token>`，再运行 `tunnel-client doctor`。
- **提示路径不允许：** 把所需目录加入 `--readable` 或 `--writable`，然后重启服务。
- **提示程序不允许：** 检查 `--executables` 白名单。只加入你信任的程序。
- **ChatGPT 看不到新工具：** 新建一个对话；仍未刷新时，删除连接器后重新添加。
- **任务运行超时：** 让 ChatGPT 使用后台任务工具，而不是短命令工具。

## 当前机器的部署说明

本仓库原本用于一台个人工作站，现有部署采用以下配置：

- `chatgpt-mcp-bridge.service` 作为 systemd 用户服务运行
- 用户 lingering 已开启，退出登录后服务仍可运行
- `tunnel-client` 和 `tailscaled` 由 systemd 管理
- `start.sh` 会优先重启 systemd 服务；找不到服务时才使用 `nohup`
- Tailscale Funnel 作为备用通道

这些设置不是运行项目的硬性要求。部署到其他电脑时，请使用自己的用户名、目录、域名和隧道配置。
