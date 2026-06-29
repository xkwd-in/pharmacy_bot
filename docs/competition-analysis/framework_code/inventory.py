"""
库存管理 + 智能补仓提示
========================
- 实时计数（每次调剂 -1）
- 低位预警（库存 < 阈值 → restock_alerts 表 + 推送药房经理）
- 效期监控（30 天内到期标记）
- 自动生成补仓单
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)


class InventoryManager:
    """SQLite 库存管理 + 补仓告警生成"""

    def __init__(self, db_path: str = "pharmacy.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # === 调剂后扣减 ===
    def decrement(self, drug_id: int, qty: int = 1) -> dict:
        """调剂成功后扣减库存，并检查是否触发补仓告警"""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE inventory SET qty_on_hand = qty_on_hand - ? WHERE drug_id = ?",
            (qty, drug_id),
        )
        self.conn.commit()
        return self._check_restock(drug_id)

    # === 补仓判断 ===
    def _check_restock(self, drug_id: int) -> dict:
        cur = self.conn.cursor()
        row = cur.execute(
            """SELECT i.qty_on_hand, i.qty_threshold, d.name
               FROM inventory i JOIN drugs d ON d.id = i.drug_id
               WHERE i.drug_id = ?""",
            (drug_id,),
        ).fetchone()
        if not row:
            return {"alert": False}

        if row["qty_on_hand"] < row["qty_threshold"]:
            self._create_alert(drug_id, row["qty_on_hand"])
            logger.warning(
                f"⚠️ 补仓告警：{row['name']} 库存 {row['qty_on_hand']} < 阈值 {row['qty_threshold']}"
            )
            return {
                "alert": True,
                "drug_name": row["name"],
                "qty_on_hand": row["qty_on_hand"],
                "threshold": row["qty_threshold"],
            }
        return {"alert": False, "qty_on_hand": row["qty_on_hand"]}

    def _create_alert(self, drug_id: int, alert_qty: int):
        """避免重复告警：同一药品若已有 new/sent 状态的告警则跳过"""
        cur = self.conn.cursor()
        existing = cur.execute(
            "SELECT id FROM restock_alerts WHERE drug_id = ? AND status IN ('new','sent')",
            (drug_id,),
        ).fetchone()
        if existing:
            return
        cur.execute(
            "INSERT INTO restock_alerts (drug_id, alert_qty, status) VALUES (?, ?, 'new')",
            (drug_id, alert_qty),
        )
        self.conn.commit()

    # === 效期监控 ===
    def check_expiring(self, days_ahead: int = 30) -> List[dict]:
        """返回未来 N 天内到期的药品列表"""
        cutoff = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        cur = self.conn.cursor()
        rows = cur.execute(
            """SELECT d.id, d.name, i.qty_on_hand, i.expiry_date
               FROM inventory i JOIN drugs d ON d.id = i.drug_id
               WHERE i.expiry_date <= ?
               ORDER BY i.expiry_date""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # === 补仓单生成 ===
    def generate_restock_orders(self) -> List[dict]:
        """收集所有 status='new' 的告警，生成补仓单（含药名/缺口量/位置）"""
        cur = self.conn.cursor()
        rows = cur.execute(
            """SELECT ra.id, d.id AS drug_id, d.name, d.storage_position,
                      i.qty_on_hand, i.qty_threshold, ra.alert_qty
               FROM restock_alerts ra
               JOIN drugs d ON d.id = ra.drug_id
               JOIN inventory i ON i.drug_id = ra.drug_id
               WHERE ra.status = 'new'""",
        ).fetchall()

        orders = []
        for r in rows:
            shortage = max(0, r["qty_threshold"] * 2 - r["qty_on_hand"])  # 补到阈值 2 倍
            orders.append({
                "alert_id": r["id"],
                "drug_id": r["drug_id"],
                "drug_name": r["name"],
                "position": r["storage_position"],
                "shortage_qty": shortage,
                "current_qty": r["qty_on_hand"],
            })

        # 标记为 sent
        if orders:
            ids = [str(o["alert_id"]) for o in orders]
            cur.execute(
                f"UPDATE restock_alerts SET status='sent' WHERE id IN ({','.join(ids)})"
            )
            self.conn.commit()
        return orders

    # === 仪表盘汇总 ===
    def dashboard(self) -> dict:
        cur = self.conn.cursor()
        total = cur.execute("SELECT COUNT(*) AS c FROM drugs").fetchone()["c"]
        low = cur.execute(
            "SELECT COUNT(*) AS c FROM inventory WHERE qty_on_hand < qty_threshold"
        ).fetchone()["c"]
        expiring = len(self.check_expiring(30))
        return {
            "total_drugs": total,
            "low_stock_count": low,
            "expiring_30d_count": expiring,
            "last_check": datetime.now().isoformat(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    inv = InventoryManager()

    print("--- 仪表盘 ---")
    print(inv.dashboard())

    print("--- 30 天内到期 ---")
    for d in inv.check_expiring(30):
        print(d)

    print("--- 模拟扣减阿莫西林（id=3，已低于阈值）---")
    print(inv.decrement(3, 1))

    print("--- 当前补仓单 ---")
    for o in inv.generate_restock_orders():
        print(o)
