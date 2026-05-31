#!/usr/bin/env python3
# encoding: utf-8
# @data:2026/05/29
# @author:pharmacy_bot
# 语音控制药房分拣
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

class VoiceControlPharmacyNode:
    def __init__(self, name):
        rospy.init_node(name, anonymous=True)
        
        self.language = os.environ['ASR_LANGUAGE']
        rospy.Subscriber('/voice_control/voice_words', String, self.words_callback)

        # 药品→颜色映射（rosparam 可覆盖，缺省用内置默认表）
        self.drug_color_map = rospy.get_param('/pharmacy/drug_color_map', None)

        # 等待分拣服务就绪
        rospy.wait_for_service('/object_sortting/enable_sortting')
        rospy.wait_for_service('/object_sortting/exit')
        rospy.wait_for_service('/object_sortting/set_color_target')
        rospy.wait_for_service('/object_sortting/set_detection_mode')
        rospy.sleep(5)

        # 设置默认模式
        self.mode = '门诊'
        rospy.set_param('/pharmacy_mode', self.mode)

        self.play('running')
        rospy.loginfo('唤醒口令: 小幻小幻(Wake up word: hello hiwonder)')
        rospy.loginfo('唤醒后15秒内可以不用再唤醒(No need to wake up within 15 seconds after waking up)')
        rospy.loginfo('药房控制指令: 开始分拣 停止分拣 紧急暂停 切换门诊/住院/养老院模式')

    def play(self, name):
        voice_play.play(name, language=self.language)

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

    def words_callback(self, msg):
        words = json.dumps(msg.data, ensure_ascii=False)[1:-1]
        if self.language == 'Chinese':
            words = words.replace(' ', '')
        print('words:', words)

        if words is not None and words not in ['唤醒成功(wake-up-success)', '休眠(Sleep)', '失败5次(Fail-5-times)', '失败10次(Fail-10-times']:
            # 优先：药品名 → 颜色 → 抓取展示
            drug_color = lookup_drug_color(words, self.drug_color_map)
            if drug_color is not None:
                drug, color = drug_color
                self.grab_drug(drug, color)
                return

            # 开始分拣
            if words == '开始分拣':
                self.play('start_sort')
                res = rospy.ServiceProxy('/object_sortting/enable_sortting', SetBool)(True)
                if res.success:
                    rospy.loginfo('分拣已开启')
                else:
                    rospy.loginfo('开启分拣失败')

            # 停止分拣
            elif words == '停止分拣':
                self.play('stop_sort')
                res = rospy.ServiceProxy('/object_sortting/enable_sortting', SetBool)(False)
                if res.success:
                    rospy.loginfo('分拣已停止')
                else:
                    rospy.loginfo('停止分拣失败')

            # 紧急暂停（退出分拣流程）
            elif words == '紧急暂停':
                self.play('emergency_stop')
                res = rospy.ServiceProxy('/object_sortting/exit', Trigger)()
                if res.success:
                    rospy.loginfo('已紧急暂停')
                else:
                    rospy.loginfo('紧急暂停失败')

            # 切换模式
            elif '切换' in words and ('门诊' in words or '住院' in words or '养老院' in words):
                if '门诊' in words:
                    self.mode = '门诊'
                elif '住院' in words:
                    self.mode = '住院'
                elif '养老院' in words:
                    self.mode = '养老院'
                rospy.set_param('/pharmacy_mode', self.mode)
                rospy.loginfo('切换模式: %s', self.mode)
                self.play('switch_mode')
            else:
                # 未知指令
                rospy.logwarn('无法识别的指令: %s', words)
                self.play('cannot_recognize')

        elif words == '休眠(Sleep)':
            rospy.sleep(0.05)

if __name__ == "__main__":
    VoiceControlPharmacyNode('voice_control_pharmacy')
    try:
        rospy.spin()
    except Exception as e:
        rospy.logerr(str(e))
        rospy.loginfo("Shutting down")
