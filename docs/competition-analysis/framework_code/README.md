# 智慧药房 SPS 框架代码（互联网+ 比赛 MVP）

> 5 个 Python 文件 + 1 个数据库 schema，构成"AI 问诊 → 视觉识别 → 机械臂 → 库存"完整闭环骨架。
> **这不是产品代码，是框架骨架**——队友按 TODO 填实就能跑。

## 文件分工

| 文件 | 角色 | 评委关注的"创新点" |
|---|---|---|
| `pharmacy_main.py` | Flask 主入口 + 路由 | 整体闭环 |
| `ai_consultant.py` | LLM 智能药师助手 | 创新点 2：AI 处方前置审核 |
| `vision_pipeline.py` | 三模态视觉融合 | 创新点 1：99.5%+ 融合识别 |
| `arm_controller.py` | ROS MoveIt 控制 | 创新点 3：4 模式调度 |
| `inventory.py` | 库存 + 补仓 | 创新点 4：自动补仓提示 |
| `schema.sql` | SQLite 表结构 | 数据持久化 |

## 快速跑通

```bash
pip install -r requirements.txt
sqlite3 pharmacy.db < schema.sql
python pharmacy_main.py
# 浏览器打开 http://localhost:5000
```

## 演示路径（队友录 demo 时严格按此走）

1. 网页输入"老人 70 岁，咳嗽 3 天" → AI 给建议（**屏幕显眼标注：仅辅助**）
2. 药师点击"确认调剂" → 系统生成 drug_list
3. 摄像头识别药品（三个识别框：pyzbar 绿 / OCR 黄 / YOLO 红）
4. 机械臂抓取动画（仿真）/ 实物（如硬件就位）
5. 库存自动 -1，低于阈值时弹出补仓提示

## 依赖说明（requirements.txt）

| 包 | 用途 | 必装 |
|---|---|---|
| flask | Web 服务 | ✅ |
| pyzbar | 条码识别 | ✅ |
| paddleocr | OCR | ✅ |
| ultralytics | YOLOv8 | ✅ |
| opencv-python | 图像处理 | ✅ |
| numpy | 数学 | ✅ |
| sqlalchemy | ORM | ✅ |
| langchain | LLM 编排 | ✅ |
| openai 或 dashscope | LLM API | ✅（任选）|
| sentence-transformers | RAG embedding | ✅ |
| faiss-cpu | 向量库 | ✅ |
| rospy | ROS 1（仿真用）| 仅 ROS 环境 |

## ROS 部分如何跑

ROS 节点（`arm_controller.py`）不能直接 `python xxx.py` 跑——需要：

```bash
# 1. 安装 ROS Noetic + MoveIt
sudo apt install ros-noetic-desktop-full ros-noetic-moveit

# 2. 把 jetarm_6dof_simulate src/ 拷到 catkin_ws
cp -r /mnt/disk200/baidu_dl/jxb/3.源码资料/src/src ~/catkin_ws/src/
cd ~/catkin_ws && catkin_make

# 3. 启动仿真
source devel/setup.bash
roslaunch jetarm_6dof_simulate gazebo.launch

# 4. 另一个终端跑我们的 arm_controller
rosrun jxb_pharmacy arm_controller.py
```

## 法律合规 ⚠️ ⚠️ ⚠️

**所有 LLM 输出都必须经"人类确认"按钮才进入下一步。**
代码里 `pharmacy_main.py` 的 `/confirm_dispense` 路由就是合规命门——不要绕过。

详见 `../08_BP写作蓝本.md` 第 11 章。
