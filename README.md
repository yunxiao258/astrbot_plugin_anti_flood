# astrbot_plugin_anti_flood

防止自动刷屏，选择性忽略其他 bot 的消息。

## 功能

1. **选择性忽略其他 bot 的消息**（三种过滤模式，`bot_filter_mode` 配置切换）
   - `all`：屏蔽所有识别为 bot 的消息
   - `trigger_only`（默认）：仅屏蔽会触发本 bot 响应的消息（@ 本 bot / 带唤醒前缀），切断 bot 互 @ 刷屏循环，bot 的普通消息放行
   - `manual_only`：仅屏蔽手动名单中的 bot（`bot_ids` + `/af mark_bot` 标记），自动探测结果不用于屏蔽
   - 识别手段：手动名单（`bot_ids`）、昵称/ID 正则（`bot_name_patterns`）、自动探测（消息扩展字段 + `get_stranger_info`，依赖 NapCat 等协议端扩展，非 OneBot v11 标准）
   - 命中后消息被静默忽略（`stop_event`），不进入 AI 决策链

2. **防刷屏检测**
   - 滑动窗口条数限制：窗口秒内发送超过 N 条消息即拦截
   - 相同内容重复限制：窗口秒内相同内容出现超过 N 次即拦截
   - 管理员与白名单用户豁免；@ 机器人的主动交互消息默认不参与检测
   - 处置方式（`flood_action` 配置）：
     - `silence`（默认）：静默拦截，不进入 AI 决策链
     - `ask_llm`：命中刷屏的消息照常进入 LLM，插件在系统提示词中注入指令说明
       （附带当前用户真实 ID，可用 `{user_id}` / `{user_name}` 占位符自定义）
       —— LLM 若决定不回答，可在回复中**隐蔽输出关键指令** `<silent />`，
       插件在 `on_llm_response` 解析后**本地决策：不将该条消息发送到群聊**，
       并始终从输出中过滤该标签。不设置任何屏蔽时间。
   - **梯度处置**（`flood_gradient`，默认开）：同一用户在 `gradient_interval_seconds`
     秒内反复刷屏时自动升级：首次按 `flood_action` 处置 → 第 2 次升级为 `ask_llm`
     给 AI 一次判断机会 → 达到 `gradient_hard_threshold` 次进入冷却期硬拦截
     （冷却期内该用户所有消息直接静默拦截 `gradient_block_minutes` 分钟）
   - **按群覆盖**（`group_overrides`）：每行 `群号:flood_action[:bot_filter_mode]`
     独立设置某群的处置方式与 bot 过滤模式，留空项使用全局默认；
     **支持热更新**（WebUI 保存即生效，无需重载插件）
   - **多平台适配**：统计键带平台前缀（onebot/wechat/telegram...），
     不同协议端的用户/群 ID 互不串扰

3. **拦截统计**：记录忽略/拦截次数，可用 `/af status` 查看；`persist_stats` 开启时
   统计持久化到 plugin_data，重启不丢失

## 安装

将本目录复制到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 插件管理中启用并重载插件。

## 配置

在 WebUI 插件管理 → 本插件 → 配置 中调整（保存后需重载插件使部分配置生效）。详细说明见 `_conf_schema.json`。

## 管理指令（仅管理员）

| 指令 | 说明 |
| --- | --- |
| `/af status` | 查看插件状态、配置与拦截统计 |
| `/af mark_bot <QQ号>` | 将某账号标记为 bot，其消息将被静默忽略 |
| `/af unmark_bot <QQ号>` | 取消 bot 标记 |
| `/af marklist` | 查看手动标记的 bot 列表 |
| `/af block <用户号> [分钟]` | 手动将用户拉入刷屏冷却 |
| `/af unblock <用户号>` | 手动解除用户的刷屏冷却 |

## 数据

- 手动标记的 bot 名单持久化在 `data/plugin_data/astrbot_plugin_anti_flood/marked_bots.json`
- 拦截统计（开启 `persist_stats` 时）持久化在 `data/plugin_data/astrbot_plugin_anti_flood/stats.json`
- 刷屏检测记录、梯度计数与 AI 提醒目标保存在内存中，重启后清零

## 注意事项

- 自动探测依赖协议端的扩展字段，NapCat 等实现可能不提供，此时需配合手动名单/正则使用
- 拦截是"静默忽略"，不会发送警告消息，避免插件自身造成刷屏
- **与 astrbot_plugin_polite_silence 共存**（已检查其源码）：
  - 指令标签不冲突：礼貌性沉默用 `<ignore id=.. duration=../>`（设置时长拒答名单），本插件用 `<silent />`（仅当前条不发送，无时长）
  - 两个插件都注入系统提示词且各自解析自己的标签，可同时启用互不干扰
  - 但为减少提示词膨胀与 LLM 指令混淆，建议二选一作为主要"AI 自主决策"机制：
    仅防刷屏 → 本插件 `flood_action: ask_llm` 足够，可将 polite_silence 的 `trigger_percent` 调低或停用
  - 若确需同开，可通过本插件的 `silent_tag` 配置自定义标签（需同步修改 `flood_llm_prompt` 中的指令描述）
