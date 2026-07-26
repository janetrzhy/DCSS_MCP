# DCSS MCP Server

让 Claude AI / ChatGPT 通过 MCP 协议玩 **Dungeon Crawl Stone Soup**。

## 架构

```
claude.ai / ChatGPT          Render Cloud
┌──────────────┐             ┌─────────────────────┐
│  MCP Client  │◄─HTTP/SSE─► │  DCSS MCP Server    │
│  (你)        │             │  ├─ pty → crawl     │
└──────────────┘             │  └─ PostgreSQL(save) │
                             └─────────────────────┘
```

## 部署到 Render

### 前置条件

- 一个 [Render](https://render.com) 账号（免费 tier 即可）
- 这个仓库 / 项目已 push 到 GitHub

### 部署步骤

#### 方式 A：一键部署（推荐）

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

这种方式会自动创建 Web Service + PostgreSQL。

#### 方式 B：手动部署

1. 把项目推到 GitHub
2. 在 Render Dashboard 点 **New +** → **Blueprint**
3. 连接你的 GitHub repo
4. Render 会自动读取 `render.yaml`，创建：
   - **Web Service** — 运行 DCSS MCP server（免费 tier）
   - **PostgreSQL** — 存存档（免费 tier，256MB）
5. 部署完成后，复制你的 **Service URL**（如 `https://dcss-mcp.onrender.com`）

### ChatGPT / 官方 MCP 连接配置

优先使用新版 Streamable HTTP 入口：

```text
https://你的render域名.onrender.com/mcp
```

连接成功后，让 ChatGPT 调用 `start_game()`，它会直接启动一个默认开局；之后用
`read_screen()` / `send_keys()` 读取屏幕和操作游戏。

### Claude / 旧 SSE 连接配置

在 Claude **Settings → Developer → MCP Servers** 中添加：

```json
{
  "mcpServers": {
    "dcss": {
      "url": "https://你的render域名.onrender.com/sse"
    }
  }
}
```

旧版 SSE 入口仍保留：

```text
https://你的render域名.onrender.com/sse
```

## MCP Tools

| Tool | 描述 |
|------|------|
| `start_game(auto_play=true)` | 开始新游戏，默认自动选 Play 进入默认开局 |
| `read_screen()` | 读取当前 DCSS 屏幕内容（80×24 纯文本） |
| `send_keys(keys)` | 发送按键，返回更新后的屏幕；支持 `Tab`、`Space`、`Enter` 这类可读按键名 |
| `start_new_game(auto_play=true)` | `start_game` 的兼容别名 |
| `save_game(slot)` | 保存当前游戏到 PostgreSQL（支持分支存档） |
| `load_game(slot)` | 从 PostgreSQL 恢复存档 |
| `list_saves()` | 列出所有存档 |
| `delete_save(slot)` | 删除存档 |
| `game_status()` | 检查 DCSS 是否在运行 |

## 玩法示例

AI 的典型游戏循环：

```
1. start_game()          → 默认开局，进入可操作游戏
2. send_keys("o")        → 自动探索 D:1
3. read_screen()         → 看发生了什么
4. send_keys("Tab")      → 攻击敌人
5. 当危险时 save_game("before_lair") → 存档保平安
6. 死了就 load_game("before_lair")   → 读档重来
```

### 推荐种族/职业

- **MiBe** (Minotaur Berserker) — 最简单，没有食物管理
- **GrEE** (Gargoyle Earth Elementalist) — 生存能力强
- **HOBe** (Half-Orc Berserker) — 也是简单粗暴

### 小技巧

- 如果屏幕出现 `--more--`，用 `send_keys(" ")`（Space）翻页
- 遇到 `(y/N)` 确认，用 `send_keys("y")` 确认
- `Tab` 键会自动攻击相邻敌人
- `o` 键自动探索（AI 最好的朋友）
- `S` 键保存退出（配合 save_game 工具使用）

## 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 确保 DCSS 已安装（macOS）
brew install crawl

# 运行（不连 PG 也能跑，只是不能存档）
set DATABASE_URL=postgresql://...  # Windows
DATABASE_URL="postgresql://..." python server.py
```

## 已知限制（Render Free Tier）

| 限制 | 影响 | 对策 |
|------|------|------|
| 512MB RAM | ✅ 够用 | — |
| 无持久磁盘 | ✅ 存档存 PG | 已解决 |
| 15min idle → spin down | ⚠️ 游戏会被中断 | 自动在关闭前存档；下次启动自动恢复 |
| Spin down 后首次请求慢 | ⚠️ 有几秒启动延迟 | 等几秒即可 |

## 协议

MIT
