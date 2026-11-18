# git-agent-history

**用一个私有 Git 仓库，备份、同步、恢复 Codex / Claude Code / DeepSeek Harness 的对话历史。**

[English](README.md)

Agent 的对话历史只存在运行它的那台机器上。换电脑、容器重建，或者被 Agent 自动清理掉
（Claude Code 默认只保留 [30 天](https://code.claude.com/docs/en/settings-reference#cleanupperioddays)），历史就没了。
`git-agent-history` 会在后台把它们备份到你自己的 GitHub 私有仓库，并且能恢复到任意一台机器上。

- **只需一个私有 Git 仓库**：不用服务器，不用注册账号。
- **每台设备一个目录**：多台设备同时同步也不会冲突；只上传新增的内容。
- **恢复成原生文件**：恢复后可以直接接着对话，还能把旧的项目路径映射到新路径。
- **默认安全**：拒绝公开仓库，提交前自动脱敏常见密钥，恢复时从不覆盖已有文件。

## 🤖 让 Agent 帮你接入（推荐）

在要备份的机器上，把下面这段话发给 Codex、Claude Code 或 DeepSeek Harness：

> 请在这台机器上配置 https://github.com/mzhangzhicheng/git-agent-history ，把我的 Agent 对话
> 自动备份到我的 GitHub 私有仓库 `你的用户名/agent-history`（不存在就创建）。
> 请按该仓库 `docs/agent-setup.md` 的步骤完成，并开启开机自启。
> 需要登录 GitHub 时让我在浏览器里授权，不要索取或输出任何 token。

每台机器都发一次即可。也可以把它装成 skill：`npx skills add mzhangzhicheng/git-agent-history`。

## 手动安装

需要 Python 3.9+、git 和 [GitHub CLI](https://cli.github.com/)。

```sh
gh auth login --web && gh auth setup-git
pipx install git+https://github.com/mzhangzhicheng/git-agent-history

git-agent-history init --repo 你的用户名/agent-history --create   # 注册本机
git-agent-history once        # 立即备份一次
git-agent-history autostart   # 后台运行并开机自启，之后每 5 分钟同步一次
git-agent-history status
```

**云端容器**（如阿里云 DSW、AutoDL）：先 `export GIT_AGENT_HISTORY_HOME=/持久化盘/.git-agent-history`，
再把 `git-agent-history startup-command` 输出的命令填进平台的启动脚本。
每次容器重建后，它会先从备份补回本机历史，再继续同步。

## 恢复

```sh
git-agent-history pull && git-agent-history devices          # 查看所有设备
git-agent-history export  --from-device 设备ID --agent codex --target ./看看   # 先导出到空目录检查
git-agent-history restore --from-device 设备ID --agent claude-code --target ~/.claude \
  --map-cwd /Users/me/code=/home/me/code                     # 项目路径变了就映射一下
```

- 已有文件不会被覆盖；只恢复一个会话可以加 `--session 会话ID`。
- 恢复到正在运行的 Agent 目录后，重启一下 Agent 即可看到。

## 支持的 Agent

| Agent | 备份的内容 |
|---|---|
| Codex | `~/.codex` 下的会话、会话索引、`state_*.sqlite` 快照 |
| Claude Code | `~/.claude/projects` 下的会话（含子 Agent）、`history.jsonl` |
| DeepSeek Harness | `~/.dsh/sessions` 下的会话日志 |

登录凭据、配置文件、插件、附件和项目代码都**不会**备份。

## 安全

- 每次上传前都会确认仓库是私有的。
- 提交前会把常见密钥（OpenAI、Anthropic、GitHub、AWS 等的 token 和私钥）替换成 `[REDACTED]`。这只是兜底，仓库仍要保持私有。
- 仓库内容没有加密，能访问仓库的人都能看到对话。

## 相关项目

- [Entire CLI](https://github.com/entireio/cli)、[SpecStory](https://github.com/specstoryai/getspecstory)：在每个项目仓库里记录 Agent 会话。
- [claude-code-sync](https://github.com/perfectra1n/claude-code-sync)、[claude-sync](https://github.com/tawanorg/claude-sync)、[repo-sessions](https://github.com/firish/repo-sessions)：同步 Claude Code（及 Codex）会话。
- [agentctxsync](https://github.com/westsource/agentctxsync)：自建服务端，多个 Agent 共享会话池。
- [cli-continues](https://github.com/yigitkonur/cli-continues)、[cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer)：把会话交给另一个 Agent 继续。

本项目只做一件事：多 Agent、多设备的整机备份，除了一个私有 Git 仓库之外什么都不需要。

更多细节见 [设计说明](docs/design.md)。

## 许可证

[MIT](LICENSE) © 2026 Zhicheng Zhang。Codex、Claude Code、DeepSeek Harness 分别属于 OpenAI、Anthropic、DeepSeek；本项目为独立项目，与它们无关联。
