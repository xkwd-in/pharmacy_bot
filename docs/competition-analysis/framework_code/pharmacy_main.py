"""
智慧药房 SPS 主入口（Flask Web 服务）
=======================================
组合 AI 智能药师 / 视觉融合 / 库存模块，对外暴露 REST + Web UI。
机械臂部分（arm_controller.py）通过 ROS 接口异步调用。

法律合规：所有 LLM 建议必须经 /confirm_dispense 这一步药师确认后才执行。
启动：python pharmacy_main.py
浏览器：http://localhost:5000
"""

import json
import logging
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

from ai_consultant import AIConsultant
from vision_pipeline import VisionPipeline
from inventory import InventoryManager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# === 单例模块 ===
consultant = AIConsultant()
inventory = InventoryManager("pharmacy.db")
vision = VisionPipeline(db_session=None, yolo_weights=None)  # MVP 演示用 stub


# =====================================
# 路由 1：AI 问诊（返回建议，未确认）
# =====================================
@app.route("/api/consult", methods=["POST"])
def consult():
    body = request.json or {}
    chief = body.get("chief_complaint", "")
    age = body.get("age")
    sex = body.get("sex")
    meds = body.get("current_meds", [])

    if not chief:
        return jsonify({"error": "缺少主诉"}), 400

    result = consultant.consult(chief, age, sex, meds)
    # 强制添加合规标识
    result["disclaimer"] = "本结果仅供药师参考。AI 不诊断、不开方，需药师确认。"
    result["timestamp"] = datetime.now().isoformat()
    return jsonify(result)


# =====================================
# 路由 2：药师确认调剂（合规命门）
# =====================================
@app.route("/api/confirm_dispense", methods=["POST"])
def confirm_dispense():
    """
    药师 review AI 建议后点确认，才进入调剂流程。
    Body: {
      "pharmacist_id": "P001",  # 药师工号
      "patient_id": "patient-xxx",
      "ai_suggestion": {...},   # 原 consult 返回
      "final_drugs": [          # 药师修正后的最终调剂清单
         {"drug_id": 1, "qty": 1},
         ...
      ]
    }
    """
    body = request.json or {}
    pharmacist_id = body.get("pharmacist_id")
    final_drugs = body.get("final_drugs", [])

    if not pharmacist_id:
        return jsonify({"error": "必须提供药师工号"}), 403
    if not final_drugs:
        return jsonify({"error": "调剂清单为空"}), 400

    # 1. 写处方记录
    conn = inventory.conn
    cur = conn.cursor()
    cur.execute("""INSERT INTO prescriptions
        (patient_id, patient_age, chief_complaint, ai_suggestion,
         final_drugs, pharmacist_id, status, confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?, 'confirmed', CURRENT_TIMESTAMP)""",
        (body.get("patient_id"), body.get("patient_age"),
         body.get("chief_complaint"),
         json.dumps(body.get("ai_suggestion"), ensure_ascii=False),
         json.dumps(final_drugs, ensure_ascii=False),
         pharmacist_id))
    rx_id = cur.lastrowid
    conn.commit()

    # 2. 推送机械臂任务（实际用 rospy.Publisher / Action client）
    arm_tasks = _build_arm_tasks(final_drugs)
    logger.info(f"推送机械臂调剂任务: rx_id={rx_id}, tasks={len(arm_tasks)}")
    # rospy.Publisher("/jxb/dispense_request", String).publish(json.dumps(arm_tasks))

    return jsonify({
        "rx_id": rx_id,
        "status": "confirmed",
        "arm_tasks": arm_tasks,
    })


# =====================================
# 路由 3：视觉识别（实时上传一张图，返回三模态融合结果）
# =====================================
@app.route("/api/recognize", methods=["POST"])
def recognize():
    import numpy as np
    import cv2
    file = request.files.get("image")
    if not file:
        return jsonify({"error": "缺少图像"}), 400

    arr = np.frombuffer(file.read(), np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "无法解析图像"}), 400

    res = vision.recognize(img)
    return jsonify({
        "drug_id": res.drug_id,
        "drug_name": res.drug_name,
        "confidence": round(res.confidence, 3),
        "detected_by": res.detected_by,
        "bbox": res.bbox,
    })


# =====================================
# 路由 4：调剂完成回执（机械臂回调）
# =====================================
@app.route("/api/dispense_done", methods=["POST"])
def dispense_done():
    body = request.json or {}
    rx_id = body.get("rx_id")
    drug_id = body.get("drug_id")
    qty = body.get("qty", 1)
    success = bool(body.get("success"))

    cur = inventory.conn.cursor()
    cur.execute("""INSERT INTO dispense_log
        (prescription_id, drug_id, qty, arm_success, vision_score)
        VALUES (?, ?, ?, ?, ?)""",
        (rx_id, drug_id, qty, success, body.get("vision_score", 0)))
    inventory.conn.commit()

    alert = None
    if success:
        alert = inventory.decrement(drug_id, qty)
    return jsonify({"ok": True, "restock_alert": alert})


# =====================================
# 路由 5：仪表盘
# =====================================
@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    summary = inventory.dashboard()
    summary["pending_restock"] = inventory.generate_restock_orders()
    summary["expiring_30d"] = inventory.check_expiring(30)
    return jsonify(summary)


# =====================================
# 路由 6：演示页面
# =====================================
@app.route("/", methods=["GET"])
def index():
    return render_template_string(DEMO_HTML)


def _build_arm_tasks(final_drugs: list) -> list:
    """把 [{drug_id, qty}] 转成机械臂任务（含 pick/place 坐标）"""
    tasks = []
    for d in final_drugs:
        # TODO: 从 drugs.storage_position 查实际坐标
        tasks.append({
            "drug_id": d["drug_id"],
            "qty": d["qty"],
            "pick_pose": {"x": 0.20, "y": 0.05, "z": 0.05},
            "place_pose": {"x": 0.30, "y": -0.05, "z": 0.05},
        })
    return tasks


DEMO_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>智慧药房 SPS 演示</title>
<style>
body{font-family:sans-serif;max-width:800px;margin:2em auto;padding:0 1em}
.card{border:1px solid #ccc;padding:1em;margin:1em 0;border-radius:6px}
button{background:#1E5DC8;color:#fff;border:0;padding:.6em 1em;border-radius:4px;cursor:pointer}
.disclaimer{color:#D9342B;font-weight:bold;background:#fef;padding:.5em;border-left:4px solid #D9342B}
</style></head>
<body>
<h1>🏥 智慧药房 SPS · 演示</h1>

<div class="card">
  <h3>第 1 步：患者咨询</h3>
  <textarea id="chief" rows="3" cols="60">70 岁老人咳嗽 3 天，无发热</textarea><br>
  <button onclick="consult()">AI 智能药师辅助归类</button>
  <pre id="result"></pre>
</div>

<div class="card">
  <h3>第 2 步：药师确认（合规命门）</h3>
  <p class="disclaimer">⚠️ AI 仅给建议，必须由执业药师按下「确认」按钮才进入调剂流程。</p>
  药师工号 <input id="pid" value="P001">
  <button onclick="confirm_dispense()">药师审核 & 确认调剂</button>
  <pre id="confirm_result"></pre>
</div>

<div class="card">
  <h3>第 3 步：机械臂调剂（ROS）</h3>
  <p>调剂任务已推送到 ROS / 机械臂控制节点（arm_controller.py）。</p>
</div>

<div class="card">
  <h3>第 4 步：库存与补仓</h3>
  <button onclick="dashboard()">刷新仪表盘</button>
  <pre id="dash"></pre>
</div>

<script>
let lastSuggestion = null;
async function consult(){
  const r = await fetch('/api/consult',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({chief_complaint:document.getElementById('chief').value, age:70})});
  lastSuggestion = await r.json();
  document.getElementById('result').innerText = JSON.stringify(lastSuggestion,null,2);
}
async function confirm_dispense(){
  if(!lastSuggestion){alert('请先咨询');return;}
  const r = await fetch('/api/confirm_dispense',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      pharmacist_id: document.getElementById('pid').value,
      patient_id:'demo-patient',
      patient_age:70,
      chief_complaint:document.getElementById('chief').value,
      ai_suggestion:lastSuggestion,
      final_drugs:[{drug_id:1,qty:1},{drug_id:5,qty:2}]
    })});
  document.getElementById('confirm_result').innerText = JSON.stringify(await r.json(),null,2);
}
async function dashboard(){
  const r = await fetch('/api/dashboard');
  document.getElementById('dash').innerText = JSON.stringify(await r.json(),null,2);
}
</script>
</body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
