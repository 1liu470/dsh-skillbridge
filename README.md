# dsh-skillbridge

让 **Hermes** 与 **DeepSeek Harness（DSH）** 共用一套技能，双向转换，装完即用。

```bash
pip install git+https://github.com/1liu470/dsh-skillbridge
skillbridge install
```

两条命令，重启 DSH，你的 Hermes 技能就出现在 DSH 的技能列表里。反过来也一样。

---

## 它解决什么问题

Hermes 和 DSH 的技能格式高度相似，但不完全一样：都在 frontmatter 用 `name` 和 `description`，都用 `SKILL.md`，但 DSH 只认五个键，且技能名必须严格 kebab-case。手工搬运时，一个字段写歪，整个技能被静默跳过——不报错，只是不出现。

更麻烦的是正文。Hermes 技能里写「用 `browser_exec` 打开页面」，到了 DSH 那边没有这个工具，技能加载成功但一执行就断。

这个工具做四件事：

1. 把 Hermes 技能转成 DSH 能直接读的形式；
2. 把 DSH 原生技能收回 Hermes；
3. 对正文引用 Hermes 专有工具的技能，自动插入跨端注记，写明该换成 DSH 的哪个工具，或者明说没有对应；
4. 转换前后都能独立复核，不只给你一句「已完成」。

---

## 为什么不需要二次转换

两侧本来就有一条共享通道：`~/.agents/skills`。

DSH 的 `@deepseek-ai/dsh-skill-filesystem` 按六个根加载技能，`~/.agents/skills` 是其中之一（rank 500）。Hermes 的 `agent/skill_utils.py` 支持 `skills.external_dirs` 配置项，指向的正是同一个目录（tier 3）。

这意味着：**只要把技能按 DSH 的格式写进这个目录，双方都能读到，不需要任何一方改代码。**

`skillbridge` 只是把「按 DSH 格式写进去」这件事做对，并检查有没有做歪。它不是补丁，而是利用既有的扩展点。

---

## 安装

### 方式一：pip（推荐）

```bash
pip install git+https://github.com/1liu470/dsh-skillbridge
skillbridge install
```

### 方式二：不安装，直接跑

```bash
git clone https://github.com/1liu470/dsh-skillbridge
cd dsh-skillbridge
python -m skillbridge install
```

无需安装依赖。装了 `PyYAML` 会用它解析 frontmatter，没装则降级为内置的极简解析器，结果一致。

### 方式三：单文件

不想装包，直接把 `skillbridge/cli.py` 拷走，`python cli.py install` 即可。它没有第三方依赖。

---

## 用法

```bash
skillbridge install            # 一条命令：检测 + 导出 + 验证
skillbridge scan               # 体检：多少能转、哪些有问题
skillbridge export             # 导出到 ~/.agents/skills
skillbridge export --force     # 覆盖重导
skillbridge export --only codex github   # 只导指定的
skillbridge export --dry-run   # 只看不写
skillbridge export --target dsh          # 写到 ~/.dsh/skills（仅 DSH 可见）
skillbridge export --include-archive     # 连 _archive 一起导
skillbridge import             # 共享根 -> Hermes
skillbridge verify             # 按 DSH 规则独立复核
skillbridge diff               # 两侧差异
```

不带 `skillbridge` 命令时，一律可用 `python -m skillbridge <子命令>` 或 `python cli.py <子命令>`。

### 两个 target 怎么选

| target | 写入位置 | DSH 可见 | Hermes 可见 |
|---|---|---|---|
| `bridge`（默认） | `~/.agents/skills` | 是 | 是 |
| `dsh` | `~/.dsh/skills` | 是 | 否 |

默认的 `bridge` 让双方都能用。想只给 DSH 用、不污染 Hermes 的视图，选 `dsh`。

---

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `HERMES_HOME` | Hermes 主目录（其下有 `skills/`） | 自动探测 `~/.hermes`、`~/AppData/Local/hermes`、`~/Library/Application Support/hermes`、`~/.local/share/hermes` |
| `DSH_HOME` | DSH 主目录 | `~/.dsh` |
| `SKILLBRIDGE_ROOT` | 共享根 | `~/.agents/skills` |

支持 Windows、Linux、macOS。路径全部由 `Path.home()` 和环境变量推导，不硬编码。

---

## DSH 的技能规则（本工具依据这些判定）

来源：`@deepseek-ai/dsh-skill-filesystem/lib/index.js` 与 `@deepseek-ai/dsh-skill/lib/index.js`，从 `app.asar` 中解出后逐行读过。

**加载根与优先级**（数值小者胜）：

| rank | 路径 |
|---:|---|
| 100 | `<项目>/.dsh/skills` |
| 200 | `<项目>/.agents/skills` |
| 300 | `customSkillDirs` 配置项 |
| 400 | `~/.dsh/skills` |
| 500 | `~/.agents/skills` |
| 600 | 内置（bundled） |

**识别规则**：

- 目录式 `<名>/SKILL.md`，或扁平 `<名>.md`；
- `name` 与 `description` 必须同时存在，缺一个整条跳过；
- `name` 必须匹配 `/^[a-z0-9]+(?:-[a-z0-9]+)*$/`；
- 认识的键只有 `name`、`description`、`whenToUse`、`disable-model-invocation`、`user-invocable`，其余静默忽略。

**注意** Hermes 的 `invocation:` 块式写法 DSH 不认，必须转成扁平的 `disable-model-invocation: true`。本工具会自动转换。

---

## 工具映射

Hermes 技能正文里引用的工具，到 DSH 侧的实际可用性：

| Hermes | DSH | 状态 |
|---|---|---|
| `skill_view` | `skill` | 对应 |
| `read_file` / `write_file` | `read` / `write` | 对应 |
| `patch` | `str_replace_editor` | 对应 |
| `search_files` | `grep` / `glob` | 对应 |
| `terminal` | `bash` / `pwsh` | 对应 |
| `web_search` | `web_search` | 同名 |
| `web_extract` | `web_fetch` | 对应 |
| `vision_analyze` | `read_image` | 大致对应 |
| `clarify` | `ask_user_question` | 对应 |
| `cronjob_manage` | `schedule_create` / `schedule_update` | 对应 |
| `execute_code` | `bash` / `pwsh` | 降级：需自行落成脚本文件 |
| `skill_manage` | `write` / `str_replace_editor` | 降级：直接改文件 |
| `browser_exec` | — | **无对应** |
| `delegate_task` | — | **无对应** |
| `text_to_speech` | — | **无对应** |
| `browser_vault_*` | — | **无对应** |

「无对应」的技能仍会被导出，正文里插入注记说明缺什么。不假装能等价转换。

---

## 已知边界

1. **六个工具 DSH 没有实现。** 依赖它们的技能导出后可以加载、可以读，但执行到那一步会断。注记会写明，不会自动改写能力。
2. **`platforms` 声明会被 DSH 丢弃。** DSH 不认这个键，所以声明「仅 linux/macos」的技能转过去后 DSH 仍会加载，运行起来才失败。`scan` 会把它们标出来。
3. **共享根是双向的。** 导出后 Hermes 也会读那里。同名技能按 tier 判定，Hermes 自有目录（tier 1）优先于共享根（tier 3），不会互相覆盖。但如果在共享根里直接改技能，下次 `export --force` 会用 Hermes 版本覆盖回去。
4. **`_archive` 默认不导。** 归档技能要显式加 `--include-archive`。
5. **本工具不修改任何一方的源码**，也不写入技能之外的任何文件。唯一动的是目标目录里的技能副本，以及该目录下的 `.skillbridge-manifest.json`。

---

## 回滚

导出没有改动 Hermes 的任何原始文件，目标目录里全是副本。

```bash
# 删除本工具导出过的全部技能（按 manifest 记录）
python -m skillbridge export --dry-run   # 先看清楚会动哪些

# 手工方式：直接删掉目标目录里的技能
rm -rf ~/.agents/skills/<技能名>

# 或整个清空（注意先备份非本工具放入的技能）
rm -rf ~/.agents/skills
```

Hermes 原始技能始终在 `$HERMES_HOME/skills/` 下，一个字节都不会被改动。

---

## 自己验证

不信上面的数字？三条命令，自己数。

```bash
# 1. 按 DSH 规则独立复核共享根
python -m skillbridge verify

# 2. 看两侧差异，区分正常转换与真实漂移
python -m skillbridge diff

# 3. 抽查任意一个技能，确认只有 frontmatter 被裁剪
head -8 ~/.agents/skills/<技能名>/SKILL.md
```

`diff` 会明确区分三种情况：完全相同、仅 frontmatter 被裁剪（正常）、正文真的漂移（异常）。正文漂移才需要你关心。

---

## 许可

MIT。见 [LICENSE](LICENSE)。
