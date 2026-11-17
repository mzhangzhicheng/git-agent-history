# git-agent-history

**用 Git 备份、同步和恢复编程 Agent 的对话历史。目前支持 Codex 和 Claude Code，计划支持更多 Agent。**

[English](README.md) · [设计说明](docs/design.md) · [给 Agent 的接入指引](docs/agent-setup.md) · [新增 Agent 支持](docs/adding-an-agent.md)

编程 Agent 把对话历史存在运行它的那台机器的本地磁盘上。云端容器重建、换电脑、
Agent 自己清理，都会让历史消失。例如 Claude Code 会删除超过
[`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference#cleanupperioddays)
天数（默认 30 天）的对话记录。

`git-agent-history` 把这些对话保存到**一个你自己的私有 Git 仓库**里。每台电脑、服务器
或容器只写自己的 `devices/<设备ID>/` 目录，互不覆盖。任意设备的历史都可以恢复到
另一台机器，恢复时还能映射项目路径。

- **只需要 Git**：一个 GitHub 私有仓库就够了，也可以用你信任的其他 git 远端。不用部署服务器、数据库，也不用注册账号。
- **按设备分目录**：每个环境只提交自己的目录；多台设备同时推送时自动 rebase 重试，从不 force push。
- **整机备份**：备份 Agent 的全部历史，不绑定某个项目，容器或磁盘被清空后照样能恢复。
- **原生格式**：恢复出来的就是 Agent 自己的文件（`*.jsonl`、`*.jsonl.zst`、Codex 的 SQLite 索引），可以直接接着用，不经过 Markdown 导出。
- **仓库不膨胀**：对话文件只追加不改写，每次检查点只存新增的行，长会话不会每 5 分钟整份重传一次。
- **默认安全**：拒绝公开仓库；提交前对常见密钥格式脱敏；恢复时从不覆盖本地已有文件。
- **对 Agent 友好**：所有命令都输出 JSON。按 [`docs/agent-setup.md`](docs/agent-setup.md) 或 [skill](skills/git-agent-history/SKILL.md)，可以让你的 Agent 替你完成接入。
- **无第三方依赖**：只需要 Python 3.9+ 标准库和 `git`，隐私检查用 GitHub CLI（`gh`）。

> 状态：**v0.1，早期版本**。已在 macOS、Linux 上测试，Windows 在 CI 中测试。
> 如果在你的 Agent 版本上遇到问题，欢迎提 issue。

## 支持的 Agent

| Agent | 备份内容 | 状态 |
|---|---|---|
| [Codex](https://developers.openai.com/codex)（CLI、IDE、App） | `sessions/`、`archived_sessions/`（`.jsonl` 和 `.jsonl.zst`）、`session_index.jsonl`、`history.jsonl`、`state_*.sqlite` 快照 | ✅ v0.1 |
| [Claude Code](https://code.claude.com/docs) | `projects/*/<会话>.jsonl`、子 Agent 对话和元数据、文本格式的工具输出、`history.jsonl` | ✅ v0.1 |
| DeepSeek Harness（`dsh`） | 会话日志 | 计划中 |
| OpenCode、Gemini CLI | 会话存储 | 计划中 |

工具不会读取登录凭据和配置：`auth.json`、`.credentials.json`、`config.toml`、
`settings.json`、插件、日志、缓存都不会被读取。完整列表见[不备份的内容](#不备份的内容)。

## 快速开始

```sh
# 1. 准备 Python 3.9+、git、GitHub CLI
gh auth login --web && gh auth setup-git

# 2. 安装（之后会发布到 PyPI，目前先从 GitHub 安装）
pipx install git+https://github.com/mzhangzhicheng/git-agent-history
#    也可以不安装，直接从克隆目录运行：
#    git clone https://github.com/mzhangzhicheng/git-agent-history && alias git-agent-history="$PWD/git-agent-history/bin/git-agent-history"

# 3. 注册本机；加 --create 时，私有仓库不存在会自动创建
git-agent-history init --repo 你的用户名/agent-history --create --label "工作笔记本"

# 4. 先手动备份一次，再让它在后台持续运行
git-agent-history once
git-agent-history start
git-agent-history autostart      # macOS launchd / Linux systemd --user / Windows 计划任务
git-agent-history status
```

`init` 会自动检测已安装的 Agent：Codex 看 `$CODEX_HOME` 或 `~/.codex`，Claude Code 看
`$CLAUDE_CONFIG_DIR` 或 `~/.claude`。也可以用 `--agents codex,claude-code`、
`--codex-home`、`--claude-home` 明确指定。后台进程默认每 5 分钟做一次检查点并推送。
由于 git 会识别 PATH 里的 `git-*` 命令，也可以写成 `git agent-history ...`。

### 让 Agent 帮你接入

在新机器上，把下面这段话发给 Codex、Claude Code 或其他 Agent：

> 请在这台机器上配置 https://github.com/mzhangzhicheng/git-agent-history，把我的编程 Agent
> 对话备份到我的 GitHub 私有仓库 `你的用户名/agent-history`。请阅读该仓库的
> `docs/agent-setup.md` 并按步骤完成。需要登录 GitHub 时，让我在浏览器里完成授权，
> 不要索取或输出任何 token。

也可以用 `npx skills add mzhangzhicheng/git-agent-history` 把它作为 skill 安装。

### 容器和云端 GPU 实例

容器镜像通常没有 systemd，重建后 `$HOME` 也会丢失。这种情况下：

1. 把状态目录放到持久化盘：`export GIT_AGENT_HISTORY_HOME=/mnt/data/.git-agent-history`，或者使用 `--state-dir`。
2. 运行 `git-agent-history startup-command`，把输出的命令填进平台的启动脚本。

每次启动时，`startup` 会在 Agent 目录为空的情况下，用**本设备自己**的备份把它补回来，
然后启动后台进程。它不会自动恢复其他设备的历史。

## 恢复

```sh
git-agent-history pull                       # 拉取其他设备的数据
git-agent-history devices                    # 查看有哪些设备
git-agent-history sessions --device old-dsw-1a2b3c4d --agent codex

# 建议先导出到一个空目录查看（Agent 运行时也安全）
git-agent-history export --from-device old-dsw-1a2b3c4d --agent codex --target ./restored

# 恢复到真实的 Agent 目录，同时映射项目路径
git-agent-history restore --from-device macbook-5e6f7a8b --agent claude-code \
  --target ~/.claude --map-cwd /Users/me/code=/home/me/code

# 只恢复一个会话
git-agent-history restore --from-device macbook-5e6f7a8b --agent codex --target ~/.codex \
  --session 0199aaaa-1111-7222-8333-444455556666
```

恢复规则：

- **从不覆盖已有文件**：内容不同的文件会列在 `conflicts_kept` 里；想对比的话，先恢复到空目录。
- **索引文件只做合并**：`session_index.jsonl` 和 `history.jsonl` 只追加目标里没有的记录。
- **Agent 运行时不动关键文件**：如果 Agent 正在运行，且目标是它正在使用的目录，会跳过索引合并和数据库恢复，只添加新的对话文件。恢复完重启 Agent 即可。
- **`--with-database`（Codex）只能用于空目标**：只有目标里没有数据库时，才会恢复保存的 SQLite 索引；会话文件路径会改到目标目录下，`--map-cwd` 还会改写项目目录。
- **Claude Code 按新路径重新归档**：`--map-cwd` 会把每个项目文件夹移到 Claude Code 对应新路径的目录名下，对话正文不做改动。
- **`--dry-run`**：只报告会发生什么，不写入任何文件。

## 工作原理

```text
你的私有仓库
├── git-agent-history.json            # 标记文件
├── README.md                          # 提醒"保持私有"
└── devices/
    ├── macbook-5e6f7a8b/
    │   ├── device.json                # 标签、系统、所属客户端 ID
    │   ├── codex/
    │   │   ├── manifest.json          # 每个文件的大小、sha256、分块、是否仍存在
    │   │   ├── files/sessions/2026/11/17/rollout-…jsonl.chunks/g001-00001.gz
    │   │   ├── files/archived_sessions/rollout-…jsonl.zst.gz
    │   │   └── databases/state_5.sqlite.gz
    │   └── claude-code/
    │       ├── manifest.json
    │       └── files/projects/-Users-me-code/<会话>.jsonl.chunks/…
    └── aliyun-dsw-1a2b3c4d/
        └── …
```

- **检查点**：每次只复制完整的行，写到一半的记录不会被保存。文件变长时，只把新增部分存成一个新的 gzip 分块；文件被重写时，用新一代分块替换旧分块，旧分块仍保留在 Git 历史里。
- **源文件删除后备份仍在**：从 Agent 目录消失的文件（例如被 Claude Code 30 天清理掉的）会留在仓库里，标记为 `present: false`，照样可以恢复。
- **数据库**：SQLite 索引通过 SQLite 的在线备份 API 复制，经过 `quick_check` 校验和脱敏，最多每 6 小时提交一次（`db_interval`）。
- **校验和**：每个存储片段在清单里都有 SHA-256，恢复时全部校验。

细节见 [docs/design.md](docs/design.md)。

## 安全与隐私

对话里可能有源代码、文件内容、命令输出以及你粘贴的任何东西，包括密钥。因此：

- `init` 和每次推送前，都会通过 `gh` 确认 GitHub 仓库是**私有**的；非 GitHub 远端必须显式加 `--trust-remote`。
- 默认的 `--secrets redact` 会在提交前，把对话和数据库快照中的常见密钥格式替换成 `[REDACTED:<类型>]`。包括私钥，OpenAI、Anthropic、GitHub、AWS、Google、Slack、Stripe、Hugging Face 的 token，以及 JWT。`warn` 只统计不替换，`off` 关闭。`git-agent-history scan` 按文件显示数量，不输出任何具体值。
- 脱敏只是兜底，不是保证：它可能漏掉密钥，也可能改动长得像密钥的普通文本。需要彻底检查时，请对仓库使用 [gitleaks](https://github.com/gitleaks/gitleaks) 等专门工具。
- 仓库内容没有加密，任何能访问这个仓库的人都能读到你的对话。
- 状态文件和错误信息不包含子进程输出（其中可能有凭据）。需要排查时，设置 `GIT_AGENT_HISTORY_DEBUG=1` 查看。

### 不备份的内容

各 Agent 的登录凭据和设置；Codex 的 `logs_*`、`goals_*`、`memories_*` 数据库；Claude Code 的
`memory/`、`file-history/`、`plugins/`；图片等附件；以及你的项目源代码（请用项目自己的 Git 仓库保存）。

## 相关项目

其他项目从不同角度处理 Agent 历史，可以按需选择，有些也能和本项目搭配使用。

| 项目 | 做法 |
|---|---|
| [Entire CLI](https://github.com/entireio/cli) | 在每个项目仓库里把 Agent 会话和提交记录关联起来，便于查看代码为什么这样改。 |
| [SpecStory](https://github.com/specstoryai/getspecstory) | 把 Agent 会话保存成 Markdown，放在项目的 `.specstory/` 目录。 |
| [claude-code-sync](https://github.com/perfectra1n/claude-code-sync) | Rust 写的命令行工具，通过 git 仓库同步 Claude Code 历史。 |
| [claude-sync](https://github.com/tawanorg/claude-sync) | 通过 Cloudflare R2 端到端加密同步 Claude Code 会话。 |
| [repo-sessions](https://github.com/firish/repo-sessions) | 按项目，通过私有 git 仓库和 git hooks 同步 Claude Code 和 Codex 会话。 |
| [agentctxsync](https://github.com/westsource/agentctxsync) | 自建服务端，让多个 Agent、多台设备共享同一个会话池。 |
| [cli-continues](https://github.com/yigitkonur/cli-continues) | 把一个 Agent 的会话交接给另一个 Agent 继续。 |
| [cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer) | 通过统一的中间格式，在不同 Agent 的会话格式之间转换。 |
| [coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search) | 为多种 Agent 的本地会话历史建立索引并搜索。 |
| [Agent Sessions](https://github.com/jazzyalex/agent-sessions) | 浏览、搜索本地 Agent 会话的 macOS 应用。 |

**为什么还要再做一个？** 本项目刻意做得很窄：整机、多 Agent、多设备的备份，只依赖一个
私有 Git 仓库，恢复出来的是原生文件。它不运行 Agent、不做格式转换、不提供界面。
想把任务从一个 Agent 交给另一个，请用上表里的交接工具。

## 路线图

- [ ] DeepSeek Harness、OpenCode、Gemini CLI 适配器
- [ ] `handoff` 命令，交给现有的跨 Agent 工具来完成
- [ ] 设备多、仓库大时，支持部分克隆和稀疏检出
- [ ] 可选的存储加密
- [ ] 发布到 PyPI

## 开发

```sh
python3 -m unittest discover -s tests -v
```

测试完全离线：只用临时目录、本地 bare 仓库和程序生成的假对话，不使用任何真实历史。
另见 [AGENTS.md](AGENTS.md) 和 [docs/adding-an-agent.md](docs/adding-an-agent.md)。

## 由来

本项目源自作者自用的私有工具 `codex-history`。当时一台云端 GPU 开发容器（阿里云 PAI-DSW）
变更配置后被重新创建，上面的 Codex 历史全部丢失，于是写了这个工具。`restore` 至今仍能读取那个工具的仓库格式。

## 许可证与商标

[MIT](LICENSE) © 2026 Zhicheng Zhang。

Codex 是 OpenAI 的产品，Claude Code 是 Anthropic 的产品。DeepSeek、OpenCode、Gemini
是其各自所有者的商标。本项目为独立项目，与上述公司无关联，也未获得其背书。
