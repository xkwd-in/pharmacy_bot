# 设计文档：语音控制按药品抓取对应颜色方块

- **日期**：2026-05-31
- **状态**：已确认，待实现
- **作者**：pharmacy_bot
- **运行环境**：真机 JetArm 六轴机械臂（ROS Noetic），**不涉及 Gazebo 仿真**

## 1. 目标

实现最小可用闭环：操作者唤醒后**用语音说出药品名**，机械臂识别对应的**纯色方块**（方块代替药品），将其**抓起并举到展示位**展示给操作者，不放入药筐。

示例：唤醒"小幻小幻" → 说"拿阿莫西林" → 机械臂抓起红色方块并举起展示。

### 范围内
- 离线语音识别药品名（3 个默认药品）。
- 药品名 → 颜色的静态映射。
- 复用现有颜色检测 + 抓取，按指定单一颜色抓取。
- 抓取后举到预设展示位停住（展示模式）。
- 语音播报反馈。

### 范围外（非目标）
- AprilTag / OCR 标签识别（代码预留扩展点，本期不实现）。
- 超过 3 种颜色 / 药品。
- LLM 参与抓取决策（见 §7）。
- Gazebo 仿真适配（仿真栈另有阻塞性问题，与本功能无关）。

## 2. 关键约束与既有事实

- **离线讯飞 ASR 是 BNF 语法约束的**：麦克风只能识别 `config/call.bnf` 中预注册的词。能识别哪些药品名只取决于该语法文件，与是否使用 LLM 无关。语法文件中已存在药品名示例（阿司匹林/布洛芬/头孢），证明药品名可被识别。
- **`object_sortting.py` 已支持单色目标**：服务 `~set_color_target`(`SetStringBool`：颜色名 + 开关) 可只针对一种颜色；`~set_detection_mode` 可切换 color/tag/OCR 模式。该节点已被 `voice_control_pharmacy.launch` 引入。
- **抓取经由 `/grasp` action server**（`hiwonder_grasp/grasp_node.py`，`MoveAction`）：`object_sortting` 发送 `mode='pick'` 与 `mode='place'` 目标。成功 pick 后默认调用 `place()` 放入固定色筐。
- `target_labels` 颜色键为 `red` / `green` / `blue`。（注：`go_home()` 内有一处把 blue 误写成 `bule` 的历史笔误，落入 else 分支，不影响功能，实现时顺手修正。）
- 真机控制全程通过自定义舵机消息 `/controllers/multi_id_pos_dur`，不经 MoveIt / ros_control。

## 3. 架构与数据流

```
唤醒"小幻小幻"
  → awake_node → asr_node 按 call.bnf 语法识别
  → 发布 /voice_control/voice_words (std_msgs/String，识别到的指令文本)
  → voice_control_pharmacy.py：在 words_callback 中匹配药品名
        ├─ 查"药品→颜色"静态映射表（默认 阿莫西林→red / 布洛芬→green / 维生素C→blue）
        ├─ 调 /object_sortting/set_detection_mode("color_only")
        ├─ 调 /object_sortting/set_color_target(<color>, True)   # 仅锁定该颜色
        ├─ 调 /object_sortting/enable_sortting(True)             # 触发一次抓取
        └─ 语音播报"已为您取出 <药品>"
  → object_sortting：检测到该色块 → start_sortting() 发 pick goal 到 /grasp
  → pick 完成（done_callback, moving_step==1, status=2）
        ├─ 若 present_only=true → present()：举到预设展示位，停住，结束本次
        └─ 否则（原行为）→ place() 放入固定色筐
```

## 4. 组件与改动清单（4 处，均为小改 + 1 处配置）

### 4.1 `src/xf_mic_asr_offline/config/call.bnf`（语法，加 1 条规则）
在 `<callstart>` 中追加药品抓取规则，保留原有指令：
```
... | (拿|取|抓取|我要|帮我拿)(阿莫西林|布洛芬|维生素C) | ...
```
讯飞 SDK 启动时由 `voice_control.cpp` 用该 BNF 重新编译语法（输出到 `config/msc/res/asr/GrmBuilld/`）。

### 4.2 药品→颜色映射（新增小配置）
默认映射（可改）：

| 药品 | 颜色键 |
|------|--------|
| 阿莫西林 | red |
| 布洛芬 | green |
| 维生素C | blue |

放置方式：写入 `voice_control_pharmacy.launch` 的 rosparam（如 `/pharmacy/drug_color_map`），由节点读取；缺省回退到内置字典。便于不改代码即可调整。

### 4.3 `src/jetarm_6dof/jetarm_6dof_app/scripts/object_sortting.py`（加展示模式）
- 新增参数 `~present_only`（bool，默认 False，保持原行为不变）。
- 在 pick 完成、原本触发 `place()` 的状态分支处增加判断：
  - `present_only=True` → 调用新增的 `present()`：把机械臂举到一个预设展示关节位姿（夹爪保持夹持），停住并干净复位状态机（`target=None`、`moving_step=0`、`stop_thread=True`），**不放下方块**。
  - `present_only=False` → 维持现有 `place()` 入筐逻辑。
- 展示位姿用一组预设舵机角度常量（具体值在真机上标定，先给安全初值）。
- 顺手修正 `go_home()` 中 `"bule"` → `"blue"` 笔误。

### 4.4 `src/xf_mic_asr_offline/scripts/voice_control_pharmacy.py`（加药品抓取分支）
- 在 `words_callback` 中追加：若识别文本命中映射表中的药品名 → 查颜色 → 依次调 4.1 数据流中的三个服务 → 语音播报。
- 启动时 `rospy.wait_for_service` 增加对 `~set_color_target` / `~set_detection_mode` 的等待。
- 保留现有"开始/停止分拣、紧急暂停、切换模式"指令（非破坏性新增）。

### 4.5 `src/xf_mic_asr_offline/launch/voice_control_pharmacy.launch`（接线）
- 给 `object_sortting.launch` 传 `present_only:=true`（或在本 launch 内 `<param>` 设置）。
- 注入 `/pharmacy/drug_color_map` 映射参数。
- 其余沿用现有 launch（base + object_sortting + mic_init + 本节点）。

## 5. 单元边界

- **语法层（call.bnf）**：定义"能听懂什么"，独立可测（编译是否通过、能否识别目标词）。
- **映射层（drug→color）**：纯函数式查表，零依赖，可离线 pytest。
- **编排层（voice_control_pharmacy.py）**：只做"识别词 → 服务调用"的翻译，不含视觉/运动逻辑。
- **抓取层（object_sortting.py）**：视觉 + 运动，唯一新增 `present()` 行为；通过 `present_only` 参数与原行为隔离，不影响既有分拣调用方。

## 6. 错误处理

- 识别到映射表外的词：播报"无法识别的指令"（复用现有 `cannot_recognize`）。
- 视野内无对应颜色：`object_sortting` 维持原超时/无目标行为；编排层不强行重试。
- 服务不可用：`wait_for_service` 阻塞 + 日志；启动顺序由 launch 保证。
- 抓取被取消/不可达：复用 `done_callback` 的 `go_home()` 回退。

## 7. LLM / API Key 处理（与本功能解耦）

- 抓取链路**全离线、确定性**，不调用 LLM。
- DeepSeek API key 仅用于既有 `ai_agent`（症状诊断/推荐），模型 `deepseek-chat`。
- key 存放于仓库根 `.env`（已被 `.gitignore` 覆盖，确认不会提交）；**绝不硬编码进任何源码或提交**。
- 该 key 曾在对话中明文出现，**建议在 DeepSeek 控制台轮换后更新 `.env`**。
- 让 `ai_agent` 实际读取 `.env`（如加 `load_dotenv()` 或由启动脚本 source）属 ai_agent 范围，作为可选后续，不在本功能实现内。

## 8. 测试计划

**离线单测（pytest，无需硬件）**
- 药品→颜色映射函数：命中、未命中、大小写/空格归一化。
- 指令解析：从识别文本中提取药品名的逻辑。

**真机分步验证**
1. 唤醒后 `rostopic echo /voice_control/voice_words` 能收到识别到的药品名。
2. 手动 `rosservice call /object_sortting/set_color_target "red" true` + `enable_sortting true` → 只抓红色。
3. `present_only` 模式：抓起后举到展示位停住、不入筐。
4. 全链路：唤醒 → 说药品名 → 抓起对应色块并展示。

**回归**
- `present_only=False` 时，原"开始分拣"批量入筐行为不变。

## 9. 风险

- **ASR 话题连通性**：现有 controller 订阅 `/voice_control/voice_words`，而 `asr_node` 发布 `~voice_words`；沿用现有 controller 同款接法，真机上需确认连通（列为验证步骤 1）。
- **展示位姿标定**：预设关节角需在真机上调试到安全且可视的姿态。
- **识别置信度**：`confidence` 默认 18，可能需按现场麦克风环境微调。
- **颜色 LAB 标定**：red/green/blue 的 LAB 范围依赖 `/config/lab`，光照变化可能需重标定（既有问题，非本功能引入）。
