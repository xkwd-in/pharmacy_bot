# 语音按药品抓取对应颜色方块 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 真机上实现「唤醒→语音说药品名→机械臂抓起对应颜色方块并举到展示位」的最小闭环。

**Architecture:** 复用离线讯飞 ASR（BNF 语法）识别药品名 → 纯逻辑映射表查到颜色 → 调 `object_sortting` 的 `set_color_target`/`set_detection_mode`/`enable_sortting` 服务锁定单色抓取 → pick 完成后走新增的「展示模式」举起停住，不入筐。LLM 不参与抓取链路。

**Tech Stack:** ROS Noetic (Python), 讯飞离线 ASR (BNF), OpenCV LAB 颜色检测, hiwonder `/grasp` MoveAction, pytest 9。

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| 新建 | `src/xf_mic_asr_offline/scripts/pharmacy_drug_map.py` | 纯逻辑：药品名→颜色映射（无 ROS 依赖，可离线单测） |
| 新建 | `src/xf_mic_asr_offline/tests/__init__.py` | 测试包标记 |
| 新建 | `src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py` | 映射逻辑单元测试 |
| 修改 | `src/xf_mic_asr_offline/config/call.bnf` | 语法加「拿/取 药品名」规则 |
| 修改 | `src/jetarm_6dof/jetarm_6dof_app/scripts/object_sortting.py` | 加 `~present_only` 参数 + `present()` 方法 + 分支 + 修 `bule` 笔误 |
| 修改 | `src/jetarm_6dof/jetarm_6dof_app/launch/object_sortting.launch` | 透传 `present_only` arg → 节点私有参数 |
| 修改 | `src/xf_mic_asr_offline/scripts/voice_control_pharmacy.py` | 加「药品名→颜色→抓取」分支 + 服务调用 |
| 修改 | `src/xf_mic_asr_offline/launch/voice_control_pharmacy.launch` | 注入 `drug_color_map` 参数 + `present_only:=true` |

---

## Task 1: 药品→颜色映射纯逻辑模块（TDD）

**Files:**
- Create: `src/xf_mic_asr_offline/tests/__init__.py`
- Create: `src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py`
- Create: `src/xf_mic_asr_offline/scripts/pharmacy_drug_map.py`

- [ ] **Step 1: 写失败测试**

创建 `src/xf_mic_asr_offline/tests/__init__.py`（空文件）。

创建 `src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py`：

```python
# encoding: utf-8
"""药品名→颜色映射的单元测试（纯逻辑，无需 ROS / 硬件）。"""
import os
import sys

# 把 scripts/ 加入 import 路径，避免依赖 catkin 安装
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pharmacy_drug_map import (  # noqa: E402
    lookup_drug_color,
    normalize,
    DEFAULT_DRUG_COLOR_MAP,
    VALID_COLORS,
)


def test_lookup_returns_color_for_known_drug():
    assert lookup_drug_color("拿阿莫西林") == ("阿莫西林", "red")


def test_lookup_handles_verb_prefixes():
    assert lookup_drug_color("我要布洛芬") == ("布洛芬", "green")


def test_lookup_case_insensitive_vitamin():
    assert lookup_drug_color("帮我拿维生素c") == ("维生素C", "blue")


def test_lookup_strips_spaces():
    assert lookup_drug_color("拿 阿 莫 西 林") == ("阿莫西林", "red")


def test_lookup_unknown_returns_none():
    assert lookup_drug_color("开始分拣") is None


def test_lookup_empty_or_none_returns_none():
    assert lookup_drug_color("") is None
    assert lookup_drug_color(None) is None


def test_default_map_colors_are_valid():
    for color in DEFAULT_DRUG_COLOR_MAP.values():
        assert color in VALID_COLORS


def test_custom_mapping_overrides_default():
    custom = {"感冒灵": "red"}
    assert lookup_drug_color("拿感冒灵", custom) == ("感冒灵", "red")
    assert lookup_drug_color("拿阿莫西林", custom) is None


def test_normalize_removes_spaces_and_handles_none():
    assert normalize("拿 阿莫西林 ") == "拿阿莫西林"
    assert normalize(None) == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/darcy/pharmacy_bot && python3 -m pytest src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'pharmacy_drug_map'`

- [ ] **Step 3: 写最小实现**

创建 `src/xf_mic_asr_offline/scripts/pharmacy_drug_map.py`：

```python
#!/usr/bin/env python3
# encoding: utf-8
"""药品名 → 颜色 的静态映射（纯逻辑，无 ROS 依赖，可离线单测）。

颜色键与 object_sortting.py 的 target_labels 保持一致：red / green / blue。
"""

# 默认映射：药品名 → 颜色键
DEFAULT_DRUG_COLOR_MAP = {
    "阿莫西林": "red",
    "布洛芬": "green",
    "维生素C": "blue",
}

# 合法颜色键（须与 object_sortting.target_labels 一致）
VALID_COLORS = ("red", "green", "blue")


def normalize(text):
    """归一化识别文本：None→空串，去除空格。"""
    if text is None:
        return ""
    return text.replace(" ", "").strip()


def lookup_drug_color(text, mapping=None):
    """从识别文本中提取药品名并返回 (drug, color)；未命中返回 None。

    采用子串包含匹配，兼容 "拿阿莫西林"、"我要布洛芬" 等带动词的指令；
    并做大小写无关匹配以兼容 "维生素C/维生素c"。
    """
    if mapping is None:
        mapping = DEFAULT_DRUG_COLOR_MAP
    norm = normalize(text)
    if not norm:
        return None
    for drug, color in mapping.items():
        drug_norm = normalize(drug)
        if drug_norm in norm or drug_norm.lower() in norm.lower():
            return (drug, color)
    return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/darcy/pharmacy_bot && python3 -m pytest src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py -v`
Expected: PASS —— 9 passed

- [ ] **Step 5: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/xf_mic_asr_offline/tests/__init__.py \
        src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py \
        src/xf_mic_asr_offline/scripts/pharmacy_drug_map.py
git commit -m "feat: 药品名→颜色静态映射模块 + 单元测试"
```

---

## Task 2: 语法加药品名识别规则

**Files:**
- Modify: `src/xf_mic_asr_offline/config/call.bnf`

> 说明：离线 ASR 只能识别 BNF 中注册的词。本任务无法离线单测（需讯飞 SDK 在硬件上重编译语法），验证放在 Task 6 真机步骤。

- [ ] **Step 1: 编辑 call.bnf**

把文件内容改为（在 `<callstart>` 末尾追加取药规则，其余保持不变）：

```
#BNF+IAT 1.0 UTF-8;
!grammar call;

!start <callstart>;

<callstart>:(开始|停止)分拣|紧急暂停|切换(门诊|住院|养老院)模式|分办(张三|李四|王五)的处方|补(阿司匹林|布洛芬|头孢)到A格|库存盘点|启动自动分拣|停止所有任务|(拿|取|抓取|我要|帮我拿)(阿莫西林|布洛芬|维生素C);
```

- [ ] **Step 2: 校验文件可读且规则完整**

Run: `cd /home/darcy/pharmacy_bot && grep -c "阿莫西林\|布洛芬\|维生素C" src/xf_mic_asr_offline/config/call.bnf`
Expected: 输出 `1`（三个药品名都在同一行规则中）

- [ ] **Step 3: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/xf_mic_asr_offline/config/call.bnf
git commit -m "feat: ASR 语法加「拿/取 药品名」识别规则"
```

---

## Task 3: object_sortting 加展示模式

**Files:**
- Modify: `src/jetarm_6dof/jetarm_6dof_app/scripts/object_sortting.py`

> 说明：ROS 节点 + 硬件依赖，无法离线单测。本任务提供精确编辑，行为验证放在 Task 6。

- [ ] **Step 1: 读取 `~present_only` 参数**

在 `__init__` 中 `self.target_labels = { ... }` 这个字典定义**之后**，紧接着加入一行：

```python
        # 展示模式：抓取后举起展示而非放入色筐（默认 False，保持原分拣行为）
        self.present_only = rospy.get_param('~present_only', False)
```

- [ ] **Step 2: 修正 go_home 中的 blue 笔误**

把 `go_home` 方法里这一行：

```python
        if self.target is not None and self.target[0] in ["bule", "tag_1"]:
```

改为：

```python
        if self.target is not None and self.target[0] in ["blue", "tag_1"]:
```

- [ ] **Step 3: 新增 present() 方法**

在 `place(self)` 方法定义**之前**插入新方法：

```python
    def present(self):
        """展示模式：抓取完成后把方块举到预设展示位并保持夹持，不放下、停止本次抓取。

        展示位关节角为初值，真机上可微调（见 Task 6 标定步骤）。
        不发送夹爪舵机(10)指令，保持 pick 时的夹持状态。
        """
        rospy.loginfo("展示模式：举起展示，不入筐")
        bus_servo_control.set_servos(
            self.servos_pub, 1500,
            ((1, 500), (2, 650), (3, 400), (4, 300), (5, 500)),
        )
        rospy.sleep(1.8)
        # 干净复位状态机（方块仍夹持在夹爪中），并停止本次分拣
        self.get_endpoint()
        self.last_position = None
        self.target = None
        self.count = 0
        self.moving_step = 0
        self.enable_sortting = False
        self.stop_thread = True
```

- [ ] **Step 4: 在状态机分支中按 present_only 选择行为**

在 `action_starting` 方法里，把这一段：

```python
            elif self.status == 2:
                self.status = 0
                self.place()
```

改为：

```python
            elif self.status == 2:
                self.status = 0
                if self.present_only:
                    self.present()
                else:
                    self.place()
```

- [ ] **Step 5: 语法自检（编译为字节码，确保无语法错误）**

Run: `cd /home/darcy/pharmacy_bot && python3 -m py_compile src/jetarm_6dof/jetarm_6dof_app/scripts/object_sortting.py && echo OK`
Expected: 输出 `OK`（仅校验语法；该文件含 ROS/cv 依赖，不在此运行节点）

- [ ] **Step 6: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/jetarm_6dof/jetarm_6dof_app/scripts/object_sortting.py
git commit -m "feat: object_sortting 增加 present_only 展示模式 + 修 blue 笔误"
```

---

## Task 4: object_sortting.launch 透传 present_only

**Files:**
- Modify: `src/jetarm_6dof/jetarm_6dof_app/launch/object_sortting.launch`

- [ ] **Step 1: 编辑 launch 加 present_only arg + param**

把文件内容改为：

```xml
<launch>
        <arg name="source_image_topic" default="/rgbd_cam/color/image_rect_color" />
        <arg name="camera_info_topic" default="/rgbd_cam/color/camera_info" />
        <arg name="present_only" default="false" />

        <node name="object_sortting" pkg="jetarm_6dof_app" type="object_sortting.py" output="screen" respawn="true">
                <param name="source_image_topic" value="$(arg source_image_topic)" />
                <param name="camera_info_topic" value="$(arg camera_info_topic)" />
                <param name="present_only" value="$(arg present_only)" />
        </node>
</launch>
```

- [ ] **Step 2: 校验 XML 格式正确**

Run: `cd /home/darcy/pharmacy_bot && python3 -c "import xml.dom.minidom,sys; xml.dom.minidom.parse('src/jetarm_6dof/jetarm_6dof_app/launch/object_sortting.launch'); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/jetarm_6dof/jetarm_6dof_app/launch/object_sortting.launch
git commit -m "feat: object_sortting.launch 透传 present_only 参数"
```

---

## Task 5: voice_control_pharmacy 加药品抓取分支

**Files:**
- Modify: `src/xf_mic_asr_offline/scripts/voice_control_pharmacy.py`

- [ ] **Step 1: 增加 import**

把文件顶部的：

```python
import os
import json
import rospy
from std_msgs.msg import String
from std_srvs.srv import Trigger, SetBool
from xf_mic_asr_offline import voice_play
```

改为：

```python
import os
import sys
import json
import rospy
from std_msgs.msg import String
from std_srvs.srv import Trigger, SetBool
from std_srvs.srv import SetString as SetStringSrv
from hiwonder_interfaces.srv import SetStringBool
from xf_mic_asr_offline import voice_play

# 让本脚本能 import 同目录下的纯逻辑模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pharmacy_drug_map import lookup_drug_color  # noqa: E402
```

- [ ] **Step 2: 在 __init__ 读映射表并等待新服务**

把 `__init__` 里：

```python
        # 等待分拣服务就绪
        rospy.wait_for_service('/object_sortting/enable_sortting')
        rospy.wait_for_service('/object_sortting/exit')
        rospy.sleep(5)
```

改为：

```python
        # 药品→颜色映射（rosparam 可覆盖，缺省用内置默认表）
        self.drug_color_map = rospy.get_param('/pharmacy/drug_color_map', None)

        # 等待分拣服务就绪
        rospy.wait_for_service('/object_sortting/enable_sortting')
        rospy.wait_for_service('/object_sortting/exit')
        rospy.wait_for_service('/object_sortting/set_color_target')
        rospy.wait_for_service('/object_sortting/set_detection_mode')
        rospy.sleep(5)
```

- [ ] **Step 3: 加 grab_drug 方法**

在 `play` 方法**之后**插入：

```python
    def grab_drug(self, drug, color):
        """锁定单一颜色并触发一次抓取（展示模式由 object_sortting 的 present_only 决定）。"""
        rospy.loginfo('收到取药指令: %s -> %s', drug, color)
        try:
            rospy.ServiceProxy('/object_sortting/set_detection_mode', SetStringSrv)('color_only')
            # 只锁定目标颜色，关闭其它颜色目标
            for c in ('red', 'green', 'blue'):
                rospy.ServiceProxy('/object_sortting/set_color_target', SetStringBool)(c, c == color)
            res = rospy.ServiceProxy('/object_sortting/enable_sortting', SetBool)(True)
            if res.success:
                rospy.loginfo('开始为 %s 抓取 %s 色块', drug, color)
                self.play('start_sort')
            else:
                rospy.logwarn('启动取药抓取失败')
                self.play('cannot_recognize')
        except rospy.ServiceException as e:
            rospy.logerr('取药服务调用失败: %s', str(e))
```

- [ ] **Step 4: 在 words_callback 里优先处理药品名**

把 `words_callback` 里这一段（识别成功后的第一个判断）：

```python
        if words is not None and words not in ['唤醒成功(wake-up-success)', '休眠(Sleep)', '失败5次(Fail-5-times)', '失败10次(Fail-10-times']:
            # 开始分拣
            if words == '开始分拣':
```

改为（在进入原有指令前先尝试药品匹配）：

```python
        if words is not None and words not in ['唤醒成功(wake-up-success)', '休眠(Sleep)', '失败5次(Fail-5-times)', '失败10次(Fail-10-times']:
            # 优先：药品名 → 颜色 → 抓取展示
            drug_color = lookup_drug_color(words, self.drug_color_map)
            if drug_color is not None:
                drug, color = drug_color
                self.grab_drug(drug, color)
                return

            # 开始分拣
            if words == '开始分拣':
```

- [ ] **Step 5: 语法自检**

Run: `cd /home/darcy/pharmacy_bot && python3 -m py_compile src/xf_mic_asr_offline/scripts/voice_control_pharmacy.py && echo OK`
Expected: 输出 `OK`

- [ ] **Step 6: 回归运行映射单测（确保 import 改动未破坏纯模块）**

Run: `cd /home/darcy/pharmacy_bot && python3 -m pytest src/xf_mic_asr_offline/tests/test_pharmacy_drug_map.py -v`
Expected: PASS —— 9 passed

- [ ] **Step 7: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/xf_mic_asr_offline/scripts/voice_control_pharmacy.py
git commit -m "feat: 语音药品名→颜色→单色抓取 链路接入 voice_control_pharmacy"
```

---

## Task 6: 启动 launch 注入映射表与展示模式

**Files:**
- Modify: `src/xf_mic_asr_offline/launch/voice_control_pharmacy.launch`

- [ ] **Step 1: 注入 drug_color_map 参数 + 给 object_sortting 传 present_only**

把 launch 里这段 object_sortting 的 include：

```xml
    <!-- 药房分拣 -->
    <include file="$(find jetarm_6dof_app)/launch/object_sortting.launch">
    	<arg name="source_image_topic" value="$(arg source_image_topic)" />
    	<arg name="camera_info_topic" value="$(arg camera_info_topic)" />
    </include>
```

改为：

```xml
    <!-- 药品→颜色映射表（可在此处增改药品；颜色须为 red/green/blue） -->
    <rosparam param="/pharmacy/drug_color_map">
      阿莫西林: red
      布洛芬: green
      维生素C: blue
    </rosparam>

    <!-- 药房分拣（present_only=true：抓起后举到展示位，不入筐） -->
    <include file="$(find jetarm_6dof_app)/launch/object_sortting.launch">
    	<arg name="source_image_topic" value="$(arg source_image_topic)" />
    	<arg name="camera_info_topic" value="$(arg camera_info_topic)" />
    	<arg name="present_only" value="true" />
    </include>
```

- [ ] **Step 2: 校验 XML 格式正确**

Run: `cd /home/darcy/pharmacy_bot && python3 -c "import xml.dom.minidom; xml.dom.minidom.parse('src/xf_mic_asr_offline/launch/voice_control_pharmacy.launch'); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 提交**

```bash
cd /home/darcy/pharmacy_bot
git add src/xf_mic_asr_offline/launch/voice_control_pharmacy.launch
git commit -m "feat: pharmacy launch 注入药品颜色映射表并启用展示模式"
```

---

## Task 7: 真机分步验证（人工，需 JetArm 硬件）

> 说明：以下步骤需在真机（Jetson + JetArm + 深度相机 + 环形麦克风）上由人操作执行。代理无法自动完成；按顺序逐项确认。

- [ ] **Step 1: 编译工作空间**

```bash
cd /home/darcy/pharmacy_bot/src && catkin_make
source ../devel/setup.bash
```
Expected: 编译无错误。

- [ ] **Step 2: 启动并验证 ASR 话题连通**

终端 A：`roslaunch xf_mic_asr_offline voice_control_pharmacy.launch`
终端 B：`rostopic echo /voice_control/voice_words`
说"小幻小幻"唤醒后说"拿阿莫西林"。
Expected: 终端 B 打印出识别到的"拿阿莫西林"。若收不到，检查 `asr_node` 的 `~voice_words` 与 `/voice_control/voice_words` 的 remap/连通（沿用其他 voice_control_* 控制器的同款接法）。

- [ ] **Step 3: 手动验证单色抓取服务**

```bash
rosservice call /object_sortting/set_detection_mode "data: 'color_only'"
rosservice call /object_sortting/set_color_target "data_str: 'red'
data_bool: true"
rosservice call /object_sortting/enable_sortting "data: true"
```
Expected: 机械臂只对红色方块发起抓取。

- [ ] **Step 4: 验证展示模式（不入筐）**

在视野放一个红色方块，执行 Step 3。
Expected: 抓起后机械臂举到展示位停住、夹爪保持夹持、**不**把方块放入色筐；`enable_sortting` 自动回到关闭。如展示位姿不理想，微调 `object_sortting.py::present()` 中 `((1,500),(2,650),(3,400),(4,300),(5,500))` 的舵机角度。

- [ ] **Step 5: 全链路验证**

唤醒"小幻小幻" → 分别说"拿阿莫西林 / 拿布洛芬 / 拿维生素C"。
Expected: 机械臂依次抓起 红 / 绿 / 蓝 方块并举起展示。

- [ ] **Step 6: 回归——批量分拣未被破坏**

用 `present_only:=false` 单独启动 `object_sortting.launch` 并 `enable_sortting true`。
Expected: 原"抓取后放入固定色筐"的批量分拣行为不变。

---

## 自检结果（Self-Review）

- **Spec 覆盖**：§4.1 语法→Task 2；§4.2 映射→Task 1+Task 6；§4.3 展示模式→Task 3；§4.4 编排→Task 5；§4.5 launch→Task 4+Task 6；§8 测试→Task 1 单测 + Task 7 真机；§7 LLM 解耦→不在抓取链路（无对应代码任务，符合设计）。无遗漏。
- **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码；展示位姿给了具体初值并标注真机微调点（非占位）。
- **类型/命名一致性**：`SetStringBool(data_str, data_bool)`（hiwonder_interfaces）、`SetStringSrv`=`std_srvs/SetString`（字段 `data`）、`DETECTION_MODE_COLOR='color_only'`、颜色键 `red/green/blue`、`present_only` 参数名、`present()` 方法名 — 跨任务一致。
