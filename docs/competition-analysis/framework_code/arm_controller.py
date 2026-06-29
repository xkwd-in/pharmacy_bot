#!/usr/bin/env python3
"""
机械臂调剂控制器（ROS Noetic + MoveIt）
=========================================
对接源码资料里的 hiwonder_grasp Action 接口 + MoveIt 运动规划。

4 种调度模式：
  - CATEGORY:    按药品大类（OTC甲/乙/Rx）分装
  - PRESCRIPTION: 按处方 drug_list 顺序抓取
  - PATIENT:     按患者一日量分装（居家养老）
  - RESTOCK:     从入库口抓到货架（库存补货）

⚠️ 此文件需在 ROS 环境运行：rosrun jxb_pharmacy arm_controller.py
不在 ROS 环境下 import 会失败。Flask 主进程通过 ROS Topic / REST API 调度本节点。
"""

import json
import logging
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# 仅在 ROS 环境下 import
try:
    import rospy
    import actionlib
    from geometry_msgs.msg import Pose, Point, Quaternion
    # 来自 src/hiwonder_grasp 的自定义消息（假设）
    from hiwonder_grasp.msg import GraspAction, GraspGoal
    from moveit_commander import MoveGroupCommander, RobotCommander, PlanningSceneInterface
    ROS_AVAILABLE = True
except ImportError:
    logger.warning("ROS 环境未就绪，arm_controller 进入 dry-run 模式")
    ROS_AVAILABLE = False


class DispenseMode(Enum):
    CATEGORY = "category"
    PRESCRIPTION = "prescription"
    PATIENT = "patient"
    RESTOCK = "restock"


@dataclass
class GraspTask:
    drug_id: int
    drug_name: str
    pick_pose: dict          # {"x": .., "y": .., "z": .., "rot": (rx,ry,rz)}
    place_pose: dict
    qty: int = 1


class ArmController:
    """JetArm 6DOF 调剂控制器，封装 MoveIt + /grasp Action"""

    def __init__(self, group_name: str = "jetarm_arm"):
        if not ROS_AVAILABLE:
            self.dry_run = True
            return
        self.dry_run = False

        rospy.init_node("jxb_arm_controller", anonymous=True)

        # MoveIt 接口
        self.robot = RobotCommander()
        self.scene = PlanningSceneInterface()
        self.group = MoveGroupCommander(group_name)
        self.group.set_planner_id("RRTstarkConfigDefault")    # RRT*
        self.group.set_planning_time(2.0)
        self.group.set_num_planning_attempts(5)

        # /grasp Action（套件原生接口）
        self.grasp_client = actionlib.SimpleActionClient("/grasp", GraspAction)
        rospy.loginfo("等待 /grasp action 服务器 ...")
        self.grasp_client.wait_for_server(rospy.Duration(10))
        rospy.loginfo("/grasp action 已就绪")

    # ============ 主入口 ============
    def dispense(self, mode: DispenseMode, tasks: List[GraspTask]) -> List[dict]:
        """
        执行一批抓取任务，返回每个任务的结果。
        失败时不抛异常——仍返回完整记录，由上游决定重试或人工介入。
        """
        results = []
        order = self._reorder_for_mode(mode, tasks)
        for task in order:
            res = self._execute_one(task)
            results.append(res)
            if not res["success"]:
                logger.warning(f"任务失败：{task.drug_name} → 等待人工介入")
                # 实际部署：发邮件/推送给值班药师，暂停流水
                break
        return results

    def _reorder_for_mode(self, mode, tasks):
        """按模式排序，最小化运动路径"""
        if mode == DispenseMode.PRESCRIPTION:
            return tasks    # 按处方原顺序
        if mode == DispenseMode.CATEGORY:
            return sorted(tasks, key=lambda t: t.pick_pose["x"])    # 按 x 优化
        if mode == DispenseMode.PATIENT:
            return tasks    # 按一日量顺序
        if mode == DispenseMode.RESTOCK:
            return sorted(tasks, key=lambda t: t.place_pose["x"])
        return tasks

    def _execute_one(self, task: GraspTask) -> dict:
        if self.dry_run:
            return self._dry_run(task)

        try:
            # 1. 规划到 pre-grasp pose（上方 10cm）
            pre_pose = self._make_pose(task.pick_pose, z_offset=0.10)
            if not self._move_to(pre_pose):
                return {"task": task.drug_name, "success": False, "step": "pre_grasp"}

            # 2. 调用 /grasp Action（套件已封装夹爪下降+闭合）
            goal = GraspGoal()
            goal.target_pose = self._make_pose(task.pick_pose)
            self.grasp_client.send_goal(goal)
            self.grasp_client.wait_for_result(rospy.Duration(15))
            grasp_res = self.grasp_client.get_result()
            if not grasp_res.success:
                return {"task": task.drug_name, "success": False, "step": "grasp"}

            # 3. 移到放置点
            place_pre = self._make_pose(task.place_pose, z_offset=0.10)
            if not self._move_to(place_pre):
                return {"task": task.drug_name, "success": False, "step": "place_pre"}

            # 4. 下降 + 释放
            place_pose = self._make_pose(task.place_pose)
            self._move_to(place_pose)
            self._release_gripper()

            return {"task": task.drug_name, "success": True}

        except Exception as e:
            logger.exception("抓取异常")
            return {"task": task.drug_name, "success": False, "error": str(e)}

    def _move_to(self, pose) -> bool:
        self.group.set_pose_target(pose)
        plan = self.group.plan()
        if not plan or not plan[0]:
            return False
        return self.group.execute(plan[1], wait=True)

    def _make_pose(self, pose_dict, z_offset: float = 0.0):
        from tf.transformations import quaternion_from_euler
        rx, ry, rz = pose_dict.get("rot", (0, 0, 0))
        q = quaternion_from_euler(rx, ry, rz)
        return Pose(
            position=Point(pose_dict["x"], pose_dict["y"], pose_dict["z"] + z_offset),
            orientation=Quaternion(*q),
        )

    def _release_gripper(self):
        # TODO: 调用 hiwonder_grasp 的 release service
        pass

    def _dry_run(self, task) -> dict:
        """无 ROS 环境时的仿真：只打 log，便于 Flask 主进程演示"""
        logger.info(f"[DRY-RUN] 抓 {task.drug_name} from {task.pick_pose} "
                    f"→ {task.place_pose}")
        return {"task": task.drug_name, "success": True, "dry_run": True}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ctrl = ArmController()
    tasks = [
        GraspTask(drug_id=1, drug_name="布洛芬",
                  pick_pose={"x": 0.20, "y": 0.05, "z": 0.05},
                  place_pose={"x": 0.30, "y": -0.05, "z": 0.05}),
        GraspTask(drug_id=2, drug_name="对乙酰氨基酚",
                  pick_pose={"x": 0.22, "y": 0.05, "z": 0.05},
                  place_pose={"x": 0.30, "y": -0.05, "z": 0.05}),
    ]
    results = ctrl.dispense(DispenseMode.PRESCRIPTION, tasks)
    print(json.dumps(results, ensure_ascii=False, indent=2))
