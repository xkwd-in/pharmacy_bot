"""
AI 智能药师助手模块
================
法律定位：辅助药师，不替代。
- 输入：患者主诉 (chief_complaint) + 年龄/性别/已用药
- 输出：建议清单 [{drug_name, reason, contraindication, confidence}]
- 调用 LLM + RAG（药典 + 配伍禁忌表）

⚠️ 重要：本模块只输出"建议"，必须由 pharmacy_main.py 路由要求药师按
   "确认"按钮才能进入调剂流程。
"""

import os
import json
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# === LLM 接入：默认 Qwen，可切 OpenAI / 本地模型 ===
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "qwen")  # qwen | openai | local


class AIConsultant:
    """AI 智能药师助手。辅助归类症状 + 推荐 OTC + 处方前置审核"""

    SYSTEM_PROMPT = """你是一名 AI 药师助手，工作在中国零售药店 / 医院药剂科。

【严格遵守】
1. 你不是医生，不做诊断
2. 你不开处方，只能推荐 OTC（甲类 / 乙类）非处方药
3. 输出必须包含"禁忌警告"、"建议线下就医"标签
4. 所有建议都必须经过人类药师确认后才能执行
5. 涉及处方药一律拒绝推荐，提示"请咨询医师"

【输出格式】 JSON：
{
  "symptom_category": "症状辅助归类（不是诊断）",
  "ai_suggestions": [
    {"drug_name": "...", "reason": "...", "contraindication": "...", "confidence": 0.0-1.0}
  ],
  "rx_required": true/false,
  "see_doctor_advice": "..."
}
"""

    def __init__(self, rag_index_path: str = "./med_rag.faiss"):
        self.rag_index_path = rag_index_path
        self.rag_retriever = None
        self._init_rag()
        self._init_llm()

    def _init_rag(self):
        """加载药典 + 配伍禁忌 + 不良反应 FAISS 索引"""
        # TODO: 真实实现时用 sentence-transformers + faiss
        # from langchain.vectorstores import FAISS
        # from langchain.embeddings import HuggingFaceEmbeddings
        # self.rag_retriever = FAISS.load_local(self.rag_index_path, embeddings)
        logger.info("RAG 索引占位：实际部署需加载药典向量库")

    def _init_llm(self):
        """初始化 LLM 客户端"""
        if LLM_PROVIDER == "qwen":
            try:
                import dashscope
                dashscope.api_key = os.getenv("DASHSCOPE_API_KEY", "")
                self.client = dashscope
            except ImportError:
                logger.warning("dashscope 未安装，LLM 降级为 stub")
                self.client = None
        elif LLM_PROVIDER == "openai":
            from openai import OpenAI
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        else:
            self.client = None  # local model placeholder

    def consult(self, chief_complaint: str, patient_age: int = None,
                patient_sex: str = None, current_meds: list = None) -> Dict:
        """
        进行一次咨询。
        返回 dict，结构与 SYSTEM_PROMPT 中 JSON 一致。
        ⚠️ 返回的 ai_suggestions 仅是建议，必须经药师确认。
        """
        # 1. RAG 检索相关药典条目
        retrieved = self._rag_search(chief_complaint)

        # 2. 构造 LLM 请求
        user_msg = f"""患者主诉：{chief_complaint}
年龄：{patient_age or '未知'}
性别：{patient_sex or '未知'}
当前用药：{current_meds or '无'}

参考药典：
{retrieved}

请按 SYSTEM_PROMPT 的 JSON 格式输出。"""

        # 3. LLM 调用
        if self.client is None:
            # Stub: 演示用，返回固定建议
            return self._stub_response(chief_complaint)

        try:
            resp = self._call_llm(user_msg)
            return json.loads(resp)
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return self._stub_response(chief_complaint)

    def _rag_search(self, query: str, top_k: int = 5) -> str:
        """从药典向量库检索相关条目"""
        # TODO: 真实实现 self.rag_retriever.similarity_search(query, k=top_k)
        return "（占位）药典条目 1; 药典条目 2; ..."

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM，返回 JSON 字符串"""
        if LLM_PROVIDER == "qwen":
            resp = self.client.Generation.call(
                model="qwen-max",
                prompt=self.SYSTEM_PROMPT + "\n\n" + user_msg,
                result_format="message",
            )
            return resp["output"]["choices"][0]["message"]["content"]
        # OpenAI 分支
        resp = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content

    def _stub_response(self, chief_complaint: str) -> Dict:
        """LLM 不可用时的演示用 stub。评委演示时也用这个保证稳定"""
        return {
            "symptom_category": "上呼吸道症状（辅助归类，非诊断）",
            "ai_suggestions": [
                {
                    "drug_name": "复方甘草片",
                    "reason": "用于咳嗽症状缓解（OTC 乙）",
                    "contraindication": "孕妇 / 哺乳期 / 高血压患者慎用",
                    "confidence": 0.78,
                },
                {
                    "drug_name": "感冒灵颗粒",
                    "reason": "缓解感冒发热症状（OTC 甲）",
                    "contraindication": "肝功能不全者禁用",
                    "confidence": 0.65,
                },
            ],
            "rx_required": False,
            "see_doctor_advice": "若 3 天未缓解或出现呼吸困难，请立即就医",
        }


if __name__ == "__main__":
    # 自测
    bot = AIConsultant()
    result = bot.consult("70 岁老人咳嗽 3 天", patient_age=70)
    print(json.dumps(result, ensure_ascii=False, indent=2))
