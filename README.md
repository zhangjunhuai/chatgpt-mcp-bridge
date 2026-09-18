# ChatGPT 云电脑桥 (v2.2)

让网页版 ChatGPT 通过自定义连接器操作这台电脑。

架构:`ChatGPT ↔ OpenAI 官方隧道(tunnel-client,出站轮询) ↔ 本机 MCP server(127.0.0.1:8000)`

- 无任何公网监听端口、无防火墙配置、无域名/DNS 依赖
- 鉴权:URL 路径里的随机 token(`token.txt` 持久化,重启不变)

## 安全模型:工作区沙箱,而非 shell 语法黑名单

| 边界 | 规则 |
|---|---|
| 写 | 仅 `--writable` 根目录(默认 `/media/zhao/add`, `~/chatgpt-downloads`, `/tmp/chatgpt-mcp-jobs`) |
| 读 | 仅 `--readable` 根目录(默认 `/media/zhao/add`, `/home/zhao`) |
| 拒绝 | `--deny` 优先(`.ssh/.gnupg/.aws/etc/boot/root/...`) |
| 执行 | argv[0] 必须在 `--executables` 白名单(51 个,含 python3/conda/git/nvidia-smi/bash...) |
| 文件命令 | `rm/mv/cp/cat/ls...` 的路径参数同样过沙箱 |
| 无 shell | argv 数组执行,`;` `&&` `|` 引号全部无害,不存在注入面 |

诚实说明:`python3`/`bash` 本身可执行任意代码,路径沙箱防的是**误操作与越界**,不是对抗性代码;token 泄露仍等于交出这台电脑的用户级权限。无 sudo。

## 13 个工具

| 工具 | 作用 |
|---|---|
| `run_command(argv, cwd, env, timeout_secs≤300)` | 执行短命令,返回 exit_code/stdout/stderr/elapsed |
| `run_python(code, cwd)` | 直接跑一段 Python,免 shell 引号 |
| `start_process(argv, cwd, env)` | 后台长任务,返回 job_id,独立进程组 |
| `check_process(job_id, tail_lines)` | 状态/时长/CPU%/RSS/GPU 显存/日志尾部 |
| `kill_process(job_id, signal)` | TERM/KILL 整进程组 |
| `read_file(path, start_line, end_line)` | 行区间读取,50000 字符 |
| `create_file(path, content, atomic)` | 仅创建新文件；目标已存在则拒绝，不覆盖 |
| `write_file(path, content, mode)` | create/overwrite/append,原子写(temp+fsync+rename) |
| `apply_patch(path, patch)` | unified diff,上下文不匹配即拒绝 |
| `list_dir(path, recursive, glob)` | 结构化条目(name/type/size/mtime/path) |
| `search_text(root, query, glob)` | 工作区文本搜索 |
| `find_files(root, glob)` | 按 glob 找文件 |
| `download_file(url, filename, dest_dir)` | http(s) 下载到工作区,≤500MB |

## 日常操作

```bash
chatgpt-mcp-bridge/start.sh          # 启动 server_v2.2.py(token 不变,URL 不变)
./test.sh <连接器URL>                 # 三步自检
sudo systemctl status tunnel-client  # 隧道客户端(systemd,开机自启)
```

服务会随系统自动启动，通常无需手动运行 `start.sh`；该脚本保留用于手动重启或更换 token。
隧道客户端与 tailscaled 同样由 systemd 常驻。

`chatgpt-mcp-bridge.service` 已作为 systemd 用户服务启用；用户 lingering 开启后，
无需登录即可随系统启动。`start.sh` 会优先重启该服务，未安装服务时才回退到 `nohup`。

## ChatGPT 端

设置 → 连接器 → 新建 → **隧道**标签 → 选 `home-mcp` → 无身份验证 → 创建。对话里开 Developer mode。
**工具清单有变时**:新开对话;不行则删连接器重建。

## 已知事项

- tailscaled 需经本机代理出网:`/etc/systemd/system/tailscaled.service.d/proxy.conf`(127.0.0.1:12000)
- tunnel-client 的 systemd 单元:`/etc/systemd/system/tunnel-client.service`(密钥在 `~/.config/tunnel-client/api-key`,600)
- Tailscale Funnel 备用路线已配置但公网 DNS 未发布(新 tailnet 问题),不影响隧道
- 长任务上限 30 分钟(run_command);实验一律 start_process + 轮询 check_process
- 画图用 matplotlib 的 savefig(无图形界面)
