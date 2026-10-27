# 双面日记（astrbot_plugin_twin_diary）

每天到点，让 AI 用自己的**当前人设**写两篇日记：

- **公开日记**：AI 知道这是写给主人看的。会发到你绑定的 QQ 私聊，有吐槽、感慨、期盼，也会留一点小秘密。
- **私密日记**：设定上「没有读者」，只写进本机的一个文件夹，不会自动发送到任何聊天。AI 以为没人会看，但文件夹在你电脑上。

两篇都会参考：**当天的聊天记录**、**长期记忆插件（我会牢牢记住你）**、**前几天的旧日记**。公开日记不会读取本插件的私密日记文件，所以私密内容不会从这条路径漏到聊天里；私密日记会读公开日记，写「没说出口的那部分」。

---

## 一、安装

1. 把整个 `astrbot_plugin_twin_diary` 文件夹放进：

   ```text
   AstrBot/data/plugins/astrbot_plugin_twin_diary
   ```

2. 打开 AstrBot WebUI → 插件 → 刷新插件列表 / 重载插件。
3. 在插件配置里确认：
   - `diary_time`：每天几点写，默认 `22:30`
   - `timezone`：默认 `Asia/Shanghai`
   - `private_dir`：私密日记保存到哪。默认在插件数据目录；要放外部文件夹，先开启 `allow_external_dirs`，再填绝对路径（外部目录不随插件备份/迁移，需自行管理）
   - 其他保持默认即可
4. 确认你的 QQ 在 AstrBot 的**管理员列表**里（WebUI → 配置 → 管理员 ID；或 `admins_id`）。
5. 用**管理员 QQ** 在要接收日记的**私聊**里发送：

   ```text
   /日记 绑定
   ```

6. 发 `/日记 状态` 确认绑定、下次时间和目录都对（该指令也只对管理员开放）。

> 不需要装额外的 Python 依赖，插件只用标准库 + AstrBot 自带接口。

---

## 二、指令

| 指令 | 作用 |
| --- | --- |
| `/日记 绑定` | 把当前私聊设为公开日记接收会话（管理员；群聊里会拒绝） |
| `/日记 状态` | 查看绑定、下次时间、今天写没写、目录、记忆插件连接、上次错误（管理员，仅私聊） |
| `/日记 写` | 立刻写今天两篇（管理员，仅私聊；会重新生成并发送，可能重复） |
| `/日记 公开 [日期]` | 查看某天的公开日记（管理员，仅私聊） |
| `/日记 私密 [日期]` | 把某天私密日记单独发到绑定私聊（管理员，仅私聊；默认关闭） |
| `/日记` 或 `/日记 帮助` | 帮助 |

日期支持 `2026-10-26`、`10-26`、`今天`、`昨天`、`前天`，留空表示今天。

---

## 三、它是怎么写的

每次运行分两步：

1. **收集素材**
   - 绑定会话最近的对话（`conversation_manager`，最多 `chat_history_limit` 条）
   - 记忆插件 `astrbot_plugin_memory_companion` 的只读 bridge（`compose_context` / `search`）
   - 前 `past_days` 天的旧日记节选
2. **两次独立的模型调用**
   - 公开日记：告诉模型「主人会看到」，要求具体、口语、有情绪，但不要真正伤人；小秘密可以只写一半。
   - 私密日记：告诉模型「不会被发送、没有读者」，可以写不满、占有欲、害怕、没说出口的话；同时把刚写的公开日记给它，让它写「没写进去的部分」。

公开日记会按段落/句子拆成几条消息发送，中间有 1.5–3.5 秒随机间隔，读起来像人在打字；私密日记只写文件。

**称谓是软推断的**：`bot_name` / `master_name` 只是默认值。提示词会明确要求优先跟随人设正文和聊天里已有的叫法，并允许同一篇日记里换称呼（比如先叫名字、后来叫「他/她」），不会为了统一而反复使用同一个词。

---

## 四、和其他插件的配合

这个插件的设计原则是**不改别人的管道、不抢别人的消息**：

- 不注册 `on_decorating_result` / `on_llm_request` 这类全局钩子，不会给别人的回复加东西。
- 不注册 LLM Tool，不影响正常聊天时模型的工具列表。
- 不改会话状态、不清上下文、不重置人格。
- 公开日记由插件直接 `context.send_message` 发送，所以：
  - 第三方的**分段回复**插件不会对日记做二次拆分（插件自己已经分好段，不会重复）；
  - 第三方的**表情包**插件不会在日记后面贴表情（如果你希望日记带表情，可以让 AI 在正文里自然用 emoji）；
  - 全局的**文转图**不会把日记变成图片，日记保持文字；
  - 第三方的**主动消息**插件不受影响，插件也不会主动去改它的时间表。为避免同一秒撞车，`jitter_seconds` 默认会在到点后随机等 0–45 秒；如果对方的主动消息有固定时段，把 `diary_time` 错开几分钟即可。
- 长期记忆插件只读：调用它的 bridge 拿记忆，不写入、不改它的数据。

如果你发现某个插件仍然打架，优先看它是不是也监听了「发送前结果」并且对**所有**消息做处理；本插件发出的日记不经过那个阶段，正常不会互相影响。

---

## 五、配置说明

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `diary_time` | `22:30` | 每天写日记的时间（HH:MM） |
| `timezone` | `Asia/Shanghai` | 时区，填 IANA 名称 |
| `jitter_seconds` | `45` | 到点后随机延迟，避免和主动消息插件撞车 |
| `catch_up_hours` | `3` | 到点时 AstrBot 没运行，启动后多久内还补写 |
| `target_session` | 空 | 一般不用填，`/日记 绑定` 会自动记录；手填会覆盖绑定，且必须是私聊 |
| `master_name` | 空 | 对那个人的默认称呼；空则用绑定昵称或「主人」；人设/上下文有更常用叫法时优先用那个 |
| `bot_name` | 空 | 日记里的默认自称；空则用人格名；人设正文里有名字时优先按人设 |
| `persona_mode` | `session` | `session` 跟随会话人格 / `fixed` 固定人格 / `custom` 自定义 |
| `persona_id` | 空 | `fixed` 时选择的人格 |
| `custom_persona` | 空 | `custom` 时使用的人设文本 |
| `style_extra` | 空 | 追加给两篇日记的额外写作要求 |
| `public_min_chars` / `public_max_chars` | 350 / 700 | 公开日记长度 |
| `private_min_chars` / `private_max_chars` | 300 / 800 | 私密日记长度 |
| `private_diary_enabled` | `true` | 是否写私密那份 |
| `include_date_line` | `true` | 正文第一行写「10月26日 星期一」 |
| `chat_history_enable` | `true` | 是否读聊天记录 |
| `chat_history_limit` | `40` | 最多读多少条聊天 |
| `chat_history_max_chars` | `2500` | 聊天素材字数上限 |
| `memory_enable` | `true` | 是否读长期记忆插件 |
| `memory_plugin_name` | `astrbot_plugin_memory_companion` | 记忆插件目录名 |
| `memory_top_k` / `memory_max_chars` / `memory_timeout` | 6 / 1500 / 20 | 记忆召回条数、字数、超时 |
| `past_days` | `3` | 参考前几天日记 |
| `past_entry_max_chars` | `220` | 每篇旧日记节选长度 |
| `allow_external_dirs` | `false` | 允许写到插件数据目录之外；默认关闭 |
| `public_dir` | 空 | 公开日记存档目录；空则插件数据目录下的 `public/` |
| `private_dir` | 空 | 私密日记目录；空则插件数据目录下的 `private/` |
| `split_public` | `true` | 公开日记分段发送 |
| `split_max_chars` | `260` | 每条消息最大字数 |
| `send_interval_min` / `send_interval_max` | 1.5 / 3.5 | 分段间隔（秒） |
| `send_retries` | `2` | 单条发送失败重试次数 |
| `provider_id` | 空 | 单独指定写日记的模型；空则用绑定会话当前模型 |
| `temperature` | `0.9` | 写作温度 |
| `max_tokens` | `1600` | 单篇最大输出 token |
| `allow_any_private_bind` | `false` | 允许非管理员绑定（不建议） |
| `allow_private_view` | `false` | 允许 `/日记 私密` 把私密日记单独发给你 |
| `notify_errors` | `false` | 失败时往绑定会话发提示 |

目录规则：留空 = 插件数据目录下的 `public/` 或 `private/`；相对路径 = 相对插件数据目录；绝对路径和 `~` 只有在 `allow_external_dirs` 开启时才允许，否则会回退到默认目录并在日志里提示。

> 数据迁移提示：插件默认把日记和 `state.json` 都放在 `data/plugin_data/astrbot_plugin_twin_diary` 下，备份/迁移 AstrBot 时可以一起带走。开启 `allow_external_dirs` 后写到 `D:\私密日记` 之类的目录，不会随插件数据备份，审计时也需要单独授权；这是为满足「私密日记放在自定义文件夹」而保留的显式选项。
>
> **升级注意**：如果你之前就把 `private_dir` / `public_dir` 设在插件数据目录之外，升级后请先把 `allow_external_dirs` 设为 `true` 再保存配置；否则插件会回退到插件数据目录，看起来像旧日记“消失”了。

> `temperature` / `max_tokens` 只在 Provider 支持时生效；AstrBot 的部分内置 Provider 会忽略这两个参数。需要严格限制输出长度时，请在 Provider 配置里使用 `custom_extra_body`。

---

## 六、隐私说明（重要）

- 自动流程**不会**把私密日记发到任何聊天，只在本地写 `.md` 文件。只有管理员主动执行 `/日记 私密 [日期]` 时，插件才会用 `context.send_message` 把当天私密日记单独发到已绑定的私聊（不经过分段/表情/文转图管线，也不进入指令回复）。这个入口默认关闭，需要时把 `allow_private_view` 设为 `true`。
- 它仍然存在于你的电脑上：能登录这台机器、能打开那个文件夹的人都能看到。这是「只有 AI 自己知道」这个设定的现实边界。
- 如果文件夹放在 OneDrive / iCloud / 坚果云等同步目录里，文件会被上传到云端。介意的话请选本地目录。Windows 下插件不会自动收紧文件 ACL，私密目录的访问权限继承所在文件夹，建议放在个人账户目录下。
- 插件自身的日志只记录路径、字数和错误摘要，不主动打印日记正文；但模型服务商仍然会收到提示词（包含聊天节选与记忆），这是任何 LLM 插件都无法避免的。
- 公开日记的提示词里明确要求「不要写真正会刺伤主人的话」，但模型不保证 100% 听话；第一次可以先 `/日记 写` 看看效果，再决定是否每天自动发。
- 公开日记也会读取记忆插件按权限返回的内容。如果你在记忆插件里存了不希望出现在公开日记里的东西，请在记忆插件侧设置可见性、敏感级别或窗口边界。

---

## 七、常见问题

**到点了没收到日记？**

1. `/日记 状态` 看是否绑定、`enabled` 是否为 true、时区是否正确、上次错误是什么。
2. 检查 AstrBot 进程当时是否在运行；超过 `catch_up_hours` 就不会补写。
3. 检查绑定会话是否还是同一个 NapCat 账号的私聊；换号或删好友后需要重新 `/日记 绑定`。
4. 看 AstrBot 日志里 `astrbot.plugin.astrbot_plugin_twin_diary` 的报错。

**私密日记在哪？**

`/日记 状态` 里的「私密目录」，或你在 `private_dir` 填的路径；文件名是 `2026-10-26.md`。

**模型写得太短 / 像作文 / 出英文？**

- 在 `style_extra` 里写具体要求，例如「每段不超过三句；不要写天气；结尾留一句明天想做的事」。
- 换一个更擅长中文写作的模型：在 `provider_id` 单独指定。
- 插件已经会在正文少于 120 字或明显像拒绝时自动重试一次；仍然不行会记录错误，可在 `/日记 状态` 看到。

**记忆插件没连上？**

`/日记 状态` 显示「未检测到」时：

- 确认 `astrbot_plugin_memory_companion` 已启用；
- 确认它的 `private_companion_bridge.enabled` 没关；
- 确认插件目录名与 `memory_plugin_name` 一致；
- 没装也不影响使用，只是少了长期记忆素材。

**会不会重复发？**

同一天自动任务只会成功写一次（状态里记 `last_public_date`）。`/日记 写` 是强制重写，会重新生成并再发一次，测试时注意。

---

## 八、开发与测试

```bash
cd astrbot_plugin_twin_diary
python -m unittest discover -s tests -t . -v
```

测试不依赖真实 AstrBot：`tests/test_plugin_flow.py` 用轻量桩跑通「收集素材 → 两次 LLM → 分段发送 → 私密落盘」的完整流程。

目录结构：

```text
astrbot_plugin_twin_diary/
  main.py                 # 插件入口、调度、指令、投递
  _conf_schema.json       # WebUI 配置
  metadata.yaml
  core/
    prompts.py            # 两套日记提示词
    memory.py             # 聊天记录 + 记忆插件 bridge
    storage.py            # 文件/状态持久化
    schedule.py           # 到点判断
    textkit.py            # 清理、分段、日期
  tests/
```

---

## 九、致谢

接口与行为参考了 [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html) 与 AstrBot 源码中的 `Context`、`PersonaManager`、`ConversationManager`、`MessageChain` 实现；长期记忆读取适配了 [astrbot_plugin_memory_companion](https://github.com/menglimi/astrbot_plugin_memory_companion) 的只读 bridge。未复制上述项目的代码。
