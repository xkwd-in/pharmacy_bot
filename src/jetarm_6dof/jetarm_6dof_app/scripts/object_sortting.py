#!/usr/bin/env python3
# coding: utf8

import os
import sys
import math
import cv2
import rospy
import numpy as np
import threading
from utils import unregister
from vision_utils import xyz_quat_to_mat, xyz_euler_to_mat, xyz_rot_to_mat, mat_to_xyz_euler, pixels_to_world, draw_tags, distance, fps, extristric_plane_shift
from sensor_msgs.msg import Image as RosImage, CameraInfo
from std_srvs.srv import Trigger, TriggerRequest, TriggerResponse
from std_srvs.srv import SetBool, SetBoolRequest, SetBoolResponse
from std_srvs.srv import SetString as SetStringSrv, SetStringResponse as SetStringSrvResponse
from hiwonder_interfaces.srv import SetStringBool, SetStringBoolRequest, SetStringBoolResponse, GetRobotPose
from hiwonder_interfaces.msg import Grasp, MoveAction, MoveGoal, MultiRawIdPosDur
from utils import unregister
from jetarm_sdk import bus_servo_control
import actions
from dt_apriltags import Detector
import actionlib
import heart
from actionlib_msgs.msg import GoalID

# ── ocr_barcode 集成 ──────────────────────────────────────────
_OCR_BARCODE_AVAILABLE = False
_drug_detector = None
try:
    _ocr_barcode_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..', 'ocr_barcode')
    if _ocr_barcode_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(_ocr_barcode_dir))
    from ocr_barcode.drug_detector import detect_drug
    _OCR_BARCODE_AVAILABLE = True
except ImportError as e:
    rospy.logwarn("ocr_barcode module not available (%s); falling back to color-only detection", str(e))

DETECTION_MODE_AUTO = 'auto'
DETECTION_MODE_BARCODE = 'barcode_only'
DETECTION_MODE_OCR = 'ocr_only'
DETECTION_MODE_COLOR = 'color_only'
DETECTION_MODE_ALL = 'all'


TARGET_POSITION = {
    "red": [0.06, 0.09],
    "green": [-0.005, 0.09],
    "blue": [-0.08, 0.09],
    "tag_1": [-0.07, 0.020],
    "tag_2": [0.0, 0.020],
    "tag_3": [0.07, 0.020]
}


CONFIG_NAME='/config'
POSITIONS_PATH='/positions'

class ObjectSorttingNode():
    def __init__(self, node_name, log_level=rospy.INFO):
        rospy.init_node(node_name, anonymous=True, log_level=log_level)

        self.K = None
        self.D = None

        self.camera_pose = None
        
        self.stop_thread = False

        self.target_position = None
        self.target_position_count = 0

        self.lock = threading.RLock()
        self.fps = fps.FPS()    # 帧率统计器(frame rate counter)
        self.thread = None

        self.enable_sortting = False
        self.imgpts = None
        self.tag_size = rospy.get_param("/config/tag_size", 0.025)
        self.entered = False
        self.center_imgpts = None
        self.roi = None
        self.pick_pitch = 80
        self.moving_step = 0
        self.status = 1
        self.count = 0
        self.size = {'width': 640, 'height':480}
        config = rospy.get_param(CONFIG_NAME)
        self.color_ranges = config['lab']
        # min_area < 物体的颜色面积 < max_area(min_area < color area of object < max_area)
        self.min_area = config['area']['min_area']
        self.max_area = config['area']['max_area']
        self.hand2cam_tf_matrix = config['hand2cam_tf_matrix'], 

        self.target = None
        self.target_labels = {
            "red": False,
            "green": False,
            "blue": False,
            "tag_1": False,
            "tag_2": False,
            "tag_3": False,
        }

        # 展示模式：抓取后举起展示而非放入色筐（默认 False，保持原分拣行为）
        self.present_only = rospy.get_param('~present_only', False)

        # ── 检测模式 ──
        self.detection_mode = DETECTION_MODE_AUTO
        self._drug_result_stable = None
        self._drug_stable_count = 0

        self.at_detector = Detector(searchpath=['apriltags'],
                       families='tag36h11',
                       nthreads=4,
                       quad_decimate=1.0,
                       quad_sigma=0.0,
                       refine_edges=1,
                       decode_sharpening=0.25,
                       debug=0)

        # sub
        self.image_sub = None
        self.camera_info_sub = None
        self.endpoint_info_sub = None
        self.servos_pub = rospy.Publisher("/controllers/multi_id_pos_dur", MultiRawIdPosDur, queue_size=1)
        self.result_image_pub = rospy.Publisher('~image_result', RosImage, queue_size=10)
        self.action_client = actionlib.SimpleActionClient('/grasp', MoveAction)

        self.cancel_pub = rospy.Publisher('/grasp/cancel', GoalID, queue_size=10)
        self.goal_id = GoalID()
        # services and topics
        self.enter_srv = rospy.Service('~enter', Trigger, self.enter_srv_callback)
        self.exit_srv =  rospy.Service('~exit', Trigger, self.exit_srv_callback)
        self.set_running_srv = rospy.Service('~enable_sortting', SetBool, self.enable_sortting_srv_callback)
        self.set_target_srv = rospy.Service('~set_color_target', SetStringBool, self.set_color_target_srv_callback)
        self.set_target_srv = rospy.Service('~set_tag_target', SetStringBool, self.set_tag_target_srv_callback)
        self.set_detection_mode_srv = rospy.Service('~set_detection_mode', SetStringSrv, self.set_detection_mode_callback)
        self.get_detection_mode_srv = rospy.Service('~get_detection_mode', Trigger, self.get_detection_mode_callback)
        self.heart = heart.Heart('~heartbeat', 5, lambda e: self.exit_srv_callback(e))

    def go_home(self):
        if self.target is not None and self.target[0] in ["blue", "tag_1"]:
            time = 1.6
        elif self.target is not None and self.target[0] in ["green", "tag_2"]:
            time = 1.3


        elif self.target is not None and self.target[0] in ["red", "tag_3"]:
            time = 1.0
        else :
            time = 1.0

        bus_servo_control.set_servos(self.servos_pub, 800, ( (2, 560), (3, 130), (4, 115), (5, 500), (10, 200)))
        rospy.sleep(1.0)
        bus_servo_control.set_servos(self.servos_pub, time*1000, ((1, 500), ))
        rospy.sleep(time)
        bus_servo_control.set_servos(self.servos_pub, 500, ((10, 200), ))
        rospy.sleep(0.5)

    def camera_info_callback(self, msg):
        with self.lock:
            K = np.matrix(msg.K).reshape(1, -1, 3)
            D = np.array(msg.D)
            new_K, roi = cv2.getOptimalNewCameraMatrix(K, D, (640, 480), 0, (640, 480))
            self.K, self.D = np.matrix(new_K), np.zeros((5, 1))


    def enter_srv_callback(self, _: TriggerRequest):
        rospy.loginfo("加载玩法")
        if self.entered:
            return TriggerResponse(success=True)
        self.entered = True

        for k, v in self.target_labels.items():
            self.target_labels[k] = False
        self.enable_sortting = False

        with self.lock:
            source_image_topic = rospy.get_param('~source_image_topic', '/camera/image_raw')
            camera_info_topic = rospy.get_param('~camera_info_topic', '/camera/camera_info')
            unregister(self.image_sub)
            unregister(self.camera_info_sub)
            self.image_sub = rospy.Subscriber(source_image_topic, RosImage, self.image_callback, queue_size=1)
            self.camera_info_sub = rospy.Subscriber(camera_info_topic, CameraInfo, self.camera_info_callback)
            threading.Thread(target=self.go_home).start()

        config = rospy.get_param(CONFIG_NAME)
        # 识别区域的四个角的世界坐标(the world coordinates of four corners in recognition region)
        white_area_cam = config['white_area_pose_cam']
        white_area_center = config['white_area_pose_world']
        self.white_area_center = white_area_center
        self.white_area_cam = white_area_cam
        white_area_height = config['white_area_world_size']['height']
        white_area_width = config['white_area_world_size']['width']
        white_area_lt = np.matmul(white_area_center, xyz_euler_to_mat((white_area_height / 2, white_area_width / 2 + 0.0, 0.0), (0, 0, 0)))
        white_area_lb = np.matmul(white_area_center, xyz_euler_to_mat((-white_area_height / 2, white_area_width / 2 + 0.0, 0.0), (0, 0, 0)))
        white_area_rb = np.matmul(white_area_center, xyz_euler_to_mat((-white_area_height / 2, -white_area_width / 2 -0.0, 0.0), (0, 0, 0)))
        white_area_rt = np.matmul(white_area_center, xyz_euler_to_mat((white_area_height / 2, -white_area_width / 2 -0.0, 0.0), (0, 0, 0)))
        endpoint = rospy.ServiceProxy('/kinematics/get_current_pose', GetRobotPose)().pose 
        self.endpoint = xyz_quat_to_mat([endpoint.position.x, endpoint.position.y,  endpoint.position.z],
                                        [endpoint.orientation.w, endpoint.orientation.x, endpoint.orientation.y, endpoint.orientation.z])
        corners_cam =  np.matmul(np.linalg.inv(np.matmul(self.endpoint, config['hand2cam_tf_matrix'])), [white_area_lt, white_area_lb, white_area_rb, white_area_rt, white_area_center])
        corners_cam = np.matmul(np.linalg.inv(white_area_cam), corners_cam)
        corners_cam = corners_cam[:, :3, 3:].reshape((-1, 3))
        tvec, rmat = config['extristric']

        while self.K is None or self.D is None:
            rospy.sleep(0.5)

        with self.lock:
            self.hand2cam_tf_matrix = config['hand2cam_tf_matrix'], 
            center_imgpts, jac = cv2.projectPoints(corners_cam[-1:], np.array(rmat), np.array(tvec), self.K, self.D)
            self.center_imgpts = np.int32(center_imgpts).reshape(2)

            tvec, rmat = extristric_plane_shift(np.array(tvec).reshape((3, 1)), np.array(rmat), 0.030)
            self.extristric = tvec, rmat
            imgpts, jac = cv2.projectPoints(corners_cam[:-1], np.array(rmat), np.array(tvec), self.K, self.D)
            self.imgpts = np.int32(imgpts).reshape(-1, 2)

            # 裁切出ROI区域(crop RIO region)
            x_min = min(self.imgpts, key=lambda p: p[0])[0] # x轴最小值(the minimum value of X-axis)
            x_max = max(self.imgpts, key=lambda p: p[0])[0] # x轴最大值(the maximum value of X-axis)
            y_min = min(self.imgpts, key=lambda p: p[1])[1] # y轴最小值(the minimum value of Y-axis)
            y_max = max(self.imgpts, key=lambda p: p[1])[1] # y轴最大值(the maximum value of Y-axis)
            roi = np.maximum(np.array([y_min, y_max, x_min, x_max]), 0)
            # print(roi)
            self.roi = roi

        return TriggerResponse(success=True)
     
    def exit_srv_callback(self, req: TriggerRequest):
        rospy.loginfo("卸载玩法")
        with self.lock:
            self.entered = False
            self.cancel_pub.publish(self.goal_id)
            rospy.loginfo("Sent cancel request")
            unregister(self.image_sub)
            unregister(self.camera_info_sub)
            self.heart.reset()
            self.enable_sortting = False
        return TriggerResponse(success=True)
    
    def enable_sortting_srv_callback(self, req: SetBoolRequest):
        with self.lock:
            if req.data:
                rospy.loginfo("开启分拣")
                self.enable_sortting = True
            else:
                rospy.loginfo("关闭分拣")
                self.cancel_pub.publish(self.goal_id)
                rospy.loginfo("Sent cancel request")
                self.enable_sortting = False
        return SetBoolResponse(success=True)

        
    def set_color_target_srv_callback(self, req: SetStringBoolRequest):
        rospy.loginfo("设置颜色目标 "+ str(req.data_str) +  str(req.data_bool))
        print(req.data_str, req.data_bool)
        if req.data_str in self.target_labels:
            self.target_labels[req.data_str] = req.data_bool
        return SetStringBoolResponse(success=True, message="")

    
    def set_tag_target_srv_callback(self, req: SetStringBoolRequest):
        rospy.loginfo("设置标签目标 " + req.data_str + " " + str(req.data_bool))
        if "tag_" + req.data_str in self.target_labels:
            self.target_labels['tag_' + req.data_str] = req.data_bool
        return SetStringBoolResponse(success=True, message="")

    # ── 检测模式切换 ──────────────────────────────────────
    def set_detection_mode_callback(self, req):
        mode = req.data
        valid_modes = [
            DETECTION_MODE_AUTO, DETECTION_MODE_BARCODE,
            DETECTION_MODE_OCR, DETECTION_MODE_COLOR, DETECTION_MODE_ALL
        ]
        if mode not in valid_modes:
            rospy.logwarn("Invalid detection mode: %s (valid: %s)", mode, valid_modes)
            return SetStringSrvResponse(success=False, message=f"Invalid mode: {mode}")
        self.detection_mode = mode
        self._drug_result_stable = None
        self._drug_stable_count = 0
        rospy.loginfo("Detection mode set to: %s", mode)
        return SetStringSrvResponse(success=True, message=f"mode={mode}")

    def get_detection_mode_callback(self, req):
        return TriggerResponse(success=True, message=self.detection_mode)

    def point_remapped(self, point, now, new, data_type=float):
        """
        将一个点的坐标从一个图片尺寸映射的新的图片上(map the coordinate of one point from a picture to a new picture of different size)
        :param point: 点的坐标(coordinate of point)
        :param now: 现在图片的尺寸(size of current picture)
        :param new: 新的图片尺寸(new picture size)
        :return: 新的点坐标(new point coordinate)
        """
        x, y = point
        now_w, now_h = now
        new_w, new_h = new
        new_x = x * new_w / now_w
        new_y = y * new_h / now_h
        return data_type(new_x), data_type(new_y)

    def adaptive_threshold(self, gray_image):
        # 用自适应阈值先进行分割, 过滤掉侧面(Segment using adaptive threshold, and filter out the side view)
        # cv2.ADAPTIVE_THRESH_MEAN_C： 邻域所有像素点的权重值是一致的(all neighboring pixel values have equal weights)
        # cv2.ADAPTIVE_THRESH_GAUSSIAN _C ： 与邻域各个像素点到中心点的距离有关，通过高斯方程得到各个点的权重值(the wights if each point are related to the distance between each neighboring pixel and the center pixel, and are calculated using the Gaussian function)
        binary = cv2.adaptiveThreshold(gray_image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 7)
        return binary
    
    def canny_proc(self, bgr_image):
        # 边缘提取，用来进一步分割(当两个相同颜色物体靠在一起时，只能靠边缘去区分)(Edge detection is used for further segmentation (when two objects of the same color are adjacent, only edges can be used to distinguish them))
        mask = cv2.Canny(bgr_image, 9, 41, 9, L2gradient=True)
        mask = 255 - cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11)))  # 加粗边界，黑白反转(thicken the edge and invert black and white)
        return mask

    def get_top_surface(self, rgb_image):
        # 为了只提取物体最上层表面(to extract only the top surface of the object)
        # 将图像转换到HSV颜色空间(convert the image to the HSV color space)
        image_scale = cv2.convertScaleAbs(rgb_image, alpha=2.5, beta=0)
        image_gray = cv2.cvtColor(image_scale, cv2.COLOR_RGB2GRAY)
        image_mb = cv2.medianBlur(image_gray, 3)  # 中值滤波(median filtering)
        image_gs = cv2.GaussianBlur(image_mb, (5, 5), 5)  # 高斯模糊去噪(Gaussian blur for noise reduction)
        binary = self.adaptive_threshold(image_gs)  # 阈值自适应(adaptive thresholding)
        mask = self.canny_proc(image_gs)  # 边缘检测(edge detection)
        mask1 = cv2.bitwise_and(binary, mask)  # 合并两个提取出来的图像，保留他们共有的地方(merge two extracted images and retain their common areas)
        roi_image_mask = cv2.bitwise_and(rgb_image, rgb_image, mask=mask1)  # 和原图做遮罩，保留需要识别的区域(mask the original image to retain the area to be recognized)
        return roi_image_mask
    
    def get_endpoint(self):
        endpoint = rospy.ServiceProxy('/kinematics/get_current_pose', GetRobotPose)().pose
        self.endpoint = xyz_quat_to_mat([endpoint.position.x, endpoint.position.y,  endpoint.position.z],
                                        [endpoint.orientation.w, endpoint.orientation.x, endpoint.orientation.y, endpoint.orientation.z])
    def done_callback(self, state, result): #动作执行完毕回调(callback for action execution completion)
        rospy.loginfo("state:%f", state)
        if not result.result.complete: # 如果在移动中被取消，需要回到初始位置(If the action is cancelled during movement, return to the initial position)
            self.count = 0
            self.go_home()

        if self.finish_percent == 1:  # 如果完整的完成移动(if the movement is completed in full)
            if self.moving_step == 1:  # 如果完成了夹取(if the gripper has completed the grasp)
                # self.moving_step = 2
                self.status = 2
            elif self.moving_step == 2:  # 如果完成了放置(if the placing is completed)
                print("放置完成")
                self.go_home()
                self.get_endpoint()
                self.last_position = None
                self.target = None
                self.count = 0
                self.moving_step = 0
                self.stop_thread = True
        else:  # 如果被取消或者无法到达指定位置(if the action is cancelled or it is unable to reach the specified position)
            self.go_home()
            self.moving_step = 0
            self.stackup_step = 0
            self.status = 0
            self.count = 0
            self.last_position = None
            self.stop_thread = True
    

    def active_callback(self): # 运动开始回调(callback movement start)
        self.start_move = True
        rospy.loginfo("start move")
    
    def feedback_callback(self, msg): # 动作执行进度回调(callback action execution progress)
        rospy.loginfo("finish action: {:.2%}".format(msg.percent))
        self.finish_percent = msg.percent;

    def present(self):
        """展示模式：抓取完成后把方块举到预设展示位并保持夹持，不放下、停止本次抓取。

        展示位关节角为初值，真机上可微调（见实现计划 Task 7 标定步骤）。
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

    def place(self):
        goal = MoveGoal()
        goal.grasp.mode = 'place'
        rospy.sleep(1)
        if 'tag' in self.target[0]:
            params = rospy.get_param(os.path.join(POSITIONS_PATH, 'tag_sortting'))
            target = params['target_' + self.target[0][-1]]
            print('target tag', target)
            goal.grasp.position.x = target[0]
            goal.grasp.position.y = target[1]
            goal.grasp.position.z = target[2]
        else:
            params = rospy.get_param(os.path.join(POSITIONS_PATH, 'color_sortting'))
            if self.target[0] == 'red':
                target_name = 'target_1'
            elif self.target[0] == 'green':
                target_name = 'target_2'
            else:
                target_name = 'target_3'
            print('target color', params[target_name])
            goal.grasp.position.x = params[target_name][0]
            goal.grasp.position.y = params[target_name][1]
            goal.grasp.position.z = params[target_name][2]

        goal.grasp.pitch = self.pick_pitch
        goal.grasp.align_angle = -90 #yaw #- 20/1000* 240
        goal.grasp.grasp_approach.z = 0.04 # 放置时靠近的方向和距离(the approaching direction and distance during placing)
        goal.grasp.grasp_retreat.z = 0.04 # 放置后后撤的方向和距离(the direction and distance of withdrawal after placing)
        goal.grasp.grasp_posture = 400  # 夹取前后夹持器的开合角度(the opening and closing angle of the gripper before and after grasping)
        goal.grasp.pre_grasp_posture = 600
        self.action_client.send_goal(goal, self.done_callback, self.active_callback, self.feedback_callback)
        self.moving_step = 2

    def start_sortting(self, pose_t, pose_R):
        rospy.loginfo("开始搬运堆叠...")
        print(pose_t, pose_R)
        self.moving_step = 1
        goal = MoveGoal()
        goal.grasp.mode = 'pick'
        # 物体坐标(object coordinate)
        goal.grasp.position.x = pose_t[0]
        goal.grasp.position.y = pose_t[1]
        goal.grasp.position.z = pose_t[2]
        # 夹取时的姿态角(the pose angle during grasping)
        goal.grasp.pitch = self.pick_pitch
        r = pose_R[2] % 90 # 将旋转角限制到 ±45°(limit the rotation angle to ±45°)
        r = r - 90 if r > 45 else (r + 90 if r < -45 else r)
        goal.grasp.align_angle = r
        #夹取时靠近的方向和距离(the direction and distance for approaching during grasping)
        goal.grasp.grasp_approach.z = 0.04
        #夹取后后撤方向和距离(the direction and distance of retreat during grasping)
        goal.grasp.grasp_retreat.z = 0.05
        #夹取前后夹持器的开合(the opening and closing of the gripper before and after the grasping)
        goal.grasp.grasp_posture = 570
        goal.grasp.pre_grasp_posture = 350
        # print("pick:",goal.grasp)
        self.action_client.send_goal(goal, self.done_callback, self.active_callback, self.feedback_callback) # 发送夹取请求(send grasping request)


    def action_starting(self, pose_t, pose_R):
        while not self.stop_thread: 
            if self.status == 1:
                self.status = 0
                self.start_sortting(pose_t,pose_R)
            elif self.status == 2:
                self.status = 0
                if self.present_only:
                    self.present()
                else:
                    self.place()
            else:
                rospy.sleep(0.01)
    def image_callback(self, ros_image):
        rgb_image = np.ndarray(shape=(ros_image.height, ros_image.width, 3), dtype=np.uint8, buffer=ros_image.data)
        # 将ros格式图像转换为opencv格式(convert the image from ros format to opencv format)
        rgb_image = np.ndarray(shape=(ros_image.height, ros_image.width, 3), dtype=np.uint8, buffer=ros_image.data)
        result_image = np.copy(rgb_image)

        # # 绘制识别区域(draw recognition region)
        # if self.imgpts is not None:
            # cv2.drawContours(result_image, [self.imgpts], -1, (255, 255, 0), 2, cv2.LINE_AA) # 绘制矩形(draw rectangle)
            # for p in self.imgpts:
                # cv2.circle(result_image, tuple(p), 8, (255, 0, 0), -1)
            # pass

        if self.center_imgpts is not None:
            cv2.line(result_image, (self.center_imgpts[0]-10, self.center_imgpts[1]), (self.center_imgpts[0]+10, self.center_imgpts[1]), (255, 255, 0), 2)
            cv2.line(result_image, (self.center_imgpts[0], self.center_imgpts[1]-10), (self.center_imgpts[0], self.center_imgpts[1]+10), (255, 255, 0), 2)
        # 生成识别区域的遮罩(generate the mask of recognition region)
        target_list = []
        index = 0
        if self.roi is not None and self.moving_step == 0:
            roi_area_mask = np.zeros(shape=(ros_image.height, ros_image.width, 1), dtype=np.uint8)
            roi_area_mask = cv2.drawContours(roi_area_mask, [self.imgpts], -1, 255, cv2.FILLED)
            rgb_image = cv2.bitwise_and(rgb_image, rgb_image, mask=roi_area_mask)  # 和原图做遮罩，保留需要识别的区域(create a mask based on the original image to retain the region to be recognized)
            roi_img = rgb_image[self.roi[0]:self.roi[1], self.roi[2]:self.roi[3]]
            roi_img = self.get_top_surface(roi_img)
            #cv2.imshow("roi", cv2.cvtColor(roi_img, cv2.COLOR_RGB2BGR))
            #cv2.waitKey(1)
            #result_image[0:int(roi_img.shape[0]), 0:int(roi_img.shape[1])] = roi_img

            # ── ocr_barcode 融合检测（在颜色检测之前）────
            drug_target = None
            if _OCR_BARCODE_AVAILABLE and self.enable_sortting:
                should_detect = True
                if self.detection_mode == DETECTION_MODE_COLOR:
                    should_detect = False
                if should_detect:
                    try:
                        drug_result = detect_drug(rgb_image, stability_frames=3, use_color_fallback=False, reset=False)
                        if drug_result and drug_result.get('method') in ('barcode', 'ocr'):
                            method = drug_result['method']
                            drug_name = drug_result.get('drug_name', '')
                            confidence = drug_result.get('confidence', 0)
                            # 画框标注
                            cv2.putText(result_image, f"{method}:{drug_name} ({confidence})",
                                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                            # 用于触发分拣
                            drug_target = {
                                'method': method,
                                'drug_name': drug_name,
                                'drug_id': drug_result.get('drug_id', ''),
                            }
                    except Exception as e:
                        rospy.logwarn_throttle(5.0, "ocr_barcode detect error: %s", str(e))

            # ── 同时做颜色检测（若模式允许）───────────────
            do_color = (self.detection_mode in (DETECTION_MODE_AUTO, DETECTION_MODE_ALL, DETECTION_MODE_COLOR)
                        or drug_target is None)

            if do_color:
                rospy.logdebug("Running color detection (mode=%s)", self.detection_mode)
                image_lab = cv2.cvtColor(roi_img, cv2.COLOR_RGB2LAB) # 转换到 LAB 空间(convert to LAB space)
                self.color_ranges = rospy.get_param('/config/lab', self.color_ranges)
                img_h, img_w = rgb_image.shape[:2]
                for color_name in ['red', 'green', 'blue']:
                    color = self.color_ranges[color_name]
                    mask = cv2.inRange(image_lab, tuple(color['min']), tuple(color['max']))   # 二值化(binarization)
                    # 平滑边缘，去除小块，合并靠近的块(Smooth edges, remove small blocks, and merge adjacent blocks)
                    eroded = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
                    dilated = cv2.dilate(eroded, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
                    contours = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[-2]  # 找出所有轮廓(find all contours)
                    contours_area = map(lambda c: (math.fabs(cv2.contourArea(c)), c), contours)  # 计算轮廓面积(calculate contour area)
                    contours = map(lambda a_c: a_c[1], filter(lambda a: self.min_area <= a[0] <= self.max_area, contours_area))
                    for c in contours:
                        # cv2.drawContours(result_image, c, -1, (255, 255, 0), 2, cv2.LINE_AA) # 绘制轮廓(draw contour)
                        rect = cv2.minAreaRect(c)  # 获取最小外接矩形(obtain the minimum bounding rectangle)
                        (center_x, center_y), _ = cv2.minEnclosingCircle(c)
                        center_x, center_y = self.roi[2] + center_x, self.roi[0] + center_y
                        #center_x, center_y = self.roi[2] + rect[0][0], self.roi[0] + rect[0][1] 
                        cv2.circle(result_image, (int(center_x), int(center_y)), 8, (0, 0, 0), -1)
                        corners = list(map(lambda p: (self.roi[2] + p[0], self.roi[0] + p[1]), cv2.boxPoints(rect))) # 获取最小外接矩形的四个角点, 转换回原始图的坐标(obtain the four corner points of the minimum rectangle and convert to the coordinates of the original image)
                        cv2.drawContours(result_image, [np.intp(corners)], -1, (0, 255, 255), 2, cv2.LINE_AA)  # 绘制矩形轮廓(draw rectangle contour)
                        index += 1 # 序号递增(incremental numbering)
                        angle = int(round(rect[2]))  # 矩形角度(rectangle angle)
                        target_list.append([color_name, index, (center_x, center_y), angle])
            else:
                rospy.logdebug("Skipping color detection (mode=%s, drug_target=%s)", self.detection_mode, drug_target)

            tags = self.at_detector.detect(cv2.cvtColor(rgb_image, cv2.COLOR_RGB2GRAY), True, (self.K[0,0], self.K[1,1], self.K[0,2], self.K[1,2]), self.tag_size)
            if len(tags) > 0:
                draw_tags(result_image, tags, corners_color=(0, 0, 255), center_color=(0, 255, 0))
                for tag in tags:
                    if 'tag_%d'%tag.tag_id in self.target_labels:
                        index += 1
                        target_list.append(['tag_%d'%tag.tag_id, index, tag])

        if self.enable_sortting and self.moving_step == 0:
            for target in target_list:
                if self.target_labels[target[0]]:
                    if not 'tag' in target[0]:
                        # 颜色处理(color processing)
                        if self.target is not None:
                            if self.target[0] == target[0]:
                                self.count += 1
                            else:
                                self.count = 0
                        self.target = target
                        if self.count > 30:
                            projection_matrix = np.row_stack((np.column_stack((self.extristric[1], self.extristric[0])), np.array([[0, 0, 0, 1]])))
                            world_pose = pixels_to_world([target[2]], self.K, projection_matrix)[0]
                            world_pose[1] = -world_pose[1]
                            world_pose[2] = 0.030
                            world_pose = np.matmul(self.white_area_center, xyz_euler_to_mat(world_pose, (0, 0, 0)))
                            world_pose[2] = 0.030
                            # print(world_pose)
                            #pose_end = np.matmul(self.hand2cam_tf_matrix, world_pose) # 转换的末端相对坐标(relative coordinate of the converted end-effector)
                            #pose_world = np.matmul(self.endpoint, pose_end) # 转换到机械臂世界坐标(convert to the world coordinate of the robotic arm)
                            pose_t, _ = mat_to_xyz_euler(world_pose)
                            pose_t[2] = 0.010
                            params = rospy.get_param(os.path.join(POSITIONS_PATH, 'color_sortting'))
                            for i in range(3):
                                pose_t[i] = pose_t[i] + params['offset'][i]
                                pose_t[i] = pose_t[i] * params['scale'][i]
                            # 高度补偿，越远重力下垂越严重补偿下垂高度(height compensation, the farther the gravity droop is more severe, compensating for droop height)
                            pose_t[2] += (math.sqrt(pose_t[1] ** 2 + pose_t[0] ** 2) - 0.15) / 0.20 * 0.025
                            # print(pose_t)
                            self.target = target
                            if self.moving_step == 0:
                                self.moving_step = 1
                                self.status = 1
                                self.stop_thread = False
                                threading.Thread(target=self.action_starting, args=(pose_t, (0, 0, target[3]))).start()
                    else:
                        # 标签处理(tag processing)
                        if self.target is not None:
                            if self.target[0] == target[0]:
                                self.count += 1
                            else:
                                self.count = 0
                        self.target = target
                        tag = target[-1]
                        pose_end = np.matmul(self.hand2cam_tf_matrix, xyz_rot_to_mat(tag.pose_t, tag.pose_R)) # 转换的末端相对坐标(relative coordinate of the converted end-effector)
                        pose_world = np.matmul(self.endpoint, pose_end) # 转换到机械臂世界坐标(convert to the world coordinate of the robotic arm)
                        pose_world_T, pose_world_euler = mat_to_xyz_euler(pose_world[0], degrees=True)
                        cv2.putText(result_image, "{:.3f} {:.3f}".format(pose_world_T[0], pose_world_T[1]), (int(tag.center[0]-50), int(tag.center[1]+22)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
                        if self.count > 50 and self.moving_step == 0:
                            self.moving_step = 1
                            print("start moving")
                            params = rospy.get_param(os.path.join(POSITIONS_PATH, 'tag_sortting'))
                            pose_world_T[2] = 0.015
                            for i in range(3):
                                pose_world_T[i] = pose_world_T[i] + params['offset'][i]
                                pose_world_T[i] = pose_world_T[i] * params['scale'][i]
                            r = pose_world_euler[2] % 90
                            r = r - 90 if r > 45 else (r + 90 if r < -45 else r)
                            pose_world_euler[2] = -r
                            self.status = 1
                            self.stop_thread = False
                            threading.Thread(target=self.action_starting, args=(pose_world_T, pose_world_euler)).start()
                    break

        # 计算帧率及发布结果图像(calculate frame rate and publish result image)
        #self.fps.update()
        #result_image = self.fps.show_fps(result_image)
        ros_image.data = result_image.tobytes()
        self.result_image_pub.publish(ros_image)



if __name__ == "__main__":
    node = ObjectSorttingNode("object_sortting", log_level=rospy.INFO)
    rospy.spin()
