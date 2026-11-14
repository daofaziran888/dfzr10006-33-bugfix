"""负载均衡策略回归测试：负责人权限、服务权重一一对应、故障转移回执。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from app.services.balance import BalanceService
from app.services.balance import _read_config
from app.store import store

BALA_0001 = 1
BALA_0002 = 2
BALA_0003 = 3
OWNER_0002 = "李架构"
ADMIN = "值班管理员"
OUTSIDER = "张运维"


class BalanceRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = BalanceService()

    def snapshot(self, entry_id: int) -> tuple:
        row = self.service.get_entry(entry_id)
        return (
            row["status"],
            row["服务列表"],
            row["权重配置"],
            row["version"],
            row["failover_count"],
        )

    # BALA-0002：非负责人切换被拒绝，服务列表与权重配置保持原值
    def test_non_owner_switch_is_rejected_and_keeps_config(self) -> None:
        before = self.snapshot(BALA_0002)
        outcome = self.service.run_action(BALA_0002, "切换策略", operator=ADMIN)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "forbidden")
        self.assertEqual(self.snapshot(BALA_0002), before)
        self.assertEqual(outcome.entry["services"], ["实时报价服务", "风控校验服务"])

    def test_owner_switch_builds_conclusion_from_current_services(self) -> None:
        outcome = self.service.run_action(BALA_0002, "切换策略", operator=OWNER_0002)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "switched")
        receipt = outcome.extra["failover"]
        self.assertEqual(receipt["服务列表"], ["实时报价服务", "风控校验服务"])
        self.assertEqual(receipt["权重配置"], {"实时报价服务": 60, "风控校验服务": 40})
        self.assertIn("当前有效服务列表", receipt["结论"])
        self.assertIn("实时报价服务:60", receipt["结论"])

    def test_repeated_switch_does_not_append_conclusion(self) -> None:
        self.service.run_action(BALA_0002, "切换策略", operator=OWNER_0002)
        outcome = self.service.run_action(BALA_0002, "切换策略", operator=OWNER_0002)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "already_switched")
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.extra["failover_count"], 1)

    def test_concurrent_switch_produces_single_effective_version(self) -> None:
        results = []

        def worker() -> None:
            results.append(self.service.run_action(BALA_0002, "切换策略", operator=OWNER_0002))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        versions = {item.extra["version"] for item in results if item.ok}
        self.assertEqual(versions, {2})
        self.assertEqual(sum(1 for item in results if item.code == "switched"), 1)
        detail = self.service.get_failover(BALA_0002)
        self.assertEqual(detail["failover_count"], 1)

    # BALA-0001：权重数量不一致时拒绝切换并回滚流量分配
    def test_weight_count_mismatch_rolls_back(self) -> None:
        before = self.snapshot(BALA_0001)
        outcome = self.service.run_action(
            BALA_0001,
            "切换策略",
            operator=ADMIN,
            values={"权重配置": "订单查询服务:40、库存查询服务:40"},
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "weight_count_mismatch")
        self.assertTrue(outcome.extra["rolled_back"])
        self.assertEqual(len(outcome.extra["traffic_allocation"]), 3)
        self.assertEqual(self.snapshot(BALA_0001), before)

    def test_unknown_weight_service_is_rejected(self) -> None:
        before = self.snapshot(BALA_0001)
        outcome = self.service.run_action(
            BALA_0001,
            "切换策略",
            operator=ADMIN,
            values={"权重配置": "订单查询服务:40、库存查询服务:40、未知服务:20"},
        )
        self.assertFalse(outcome.ok)
        error_codes = {item["code"] for item in outcome.extra["errors"]}
        self.assertIn("weight_missing", error_codes)
        self.assertIn("weight_unknown_service", error_codes)
        messages = "；".join(item["message"] for item in outcome.extra["errors"])
        self.assertIn("支付查询服务", messages)
        self.assertIn("未知服务", messages)
        self.assertEqual(self.snapshot(BALA_0001), before)

    # BALA-0003：完成态重复切换不追加结论
    def test_completed_policy_repeat_switch_does_not_append(self) -> None:
        detail_before = self.service.get_failover(BALA_0003)
        self.assertEqual(detail_before["failover_count"], 1)

        outcome = self.service.run_action(BALA_0003, "切换策略", operator=ADMIN)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "already_switched")
        self.assertEqual(outcome.extra["failover_count"], 1)

        detail_after = self.service.get_failover(BALA_0003)
        self.assertEqual(detail_after["failover_count"], 1)
        self.assertEqual(detail_after["conclusion"]["服务列表"], ["会员查询服务", "积分查询服务", "权益查询服务"])
        self.assertTrue(detail_after["receipt_matches_current"])

    def test_completed_policy_rejects_config_change(self) -> None:
        before = self.snapshot(BALA_0003)
        outcome = self.service.run_action(
            BALA_0003,
            "切换策略",
            operator=ADMIN,
            values={"服务列表": "会员查询服务"},
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "policy_completed")
        self.assertEqual(self.snapshot(BALA_0003), before)

    # 历史数据兼容：不同时期的服务列表 / 权重配置写法都要能读成同一份服务权重
    def test_legacy_formats_are_supported(self) -> None:
        cases = [
            ({"服务列表": "a、b", "权重配置": "a:1、b:2"}, ["a", "b"], {"a": 1, "b": 2}),
            ({"服务列表": "a,b", "权重配置": "a=1;b=3"}, ["a", "b"], {"a": 1, "b": 3}),
            ({"服务列表": ["a", "b"], "权重配置": [1, 2]}, ["a", "b"], {"a": 1, "b": 2}),
            ({"服务列表": ["a", "b"], "权重配置": [["a", 1], ["b", 2]]}, ["a", "b"], {"a": 1, "b": 2}),
            ({"服务列表": "a、b", "权重配置": {"a": 1, "b": 2}}, ["a", "b"], {"a": 1, "b": 2}),
            ({"services": ["a", "b"], "weights": {"a": 10, "b": 20}}, ["a", "b"], {"a": 10, "b": 20}),
            (
                {
                    "服务列表": '[{"服务":"a","权重":5},{"服务":"b","权重":7}]',
                    "权重配置": '[{"服务":"a","权重":5},{"服务":"b","权重":7}]',
                },
                ["a", "b"],
                {"a": 5, "b": 7},
            ),
        ]
        for row, services, weights in cases:
            with self.subTest(row=row):
                parsed_services, parsed_weights, errors = _read_config(row)
                self.assertEqual(parsed_services, services)
                self.assertEqual(parsed_weights, weights)
                self.assertEqual(errors, [])

    def test_list_exposes_same_service_weights_as_detail(self) -> None:
        items, _ = self.service.list_entries(page=1, size=20)
        listed = {item["策略编号"]: item["service_weights"] for item in items}
        for entry_id, code in ((BALA_0001, "BALA-0001"), (BALA_0002, "BALA-0002"), (BALA_0003, "BALA-0003")):
            detail = self.service.get_failover(entry_id)
            self.assertEqual(listed[code], detail["service_weights"])


if __name__ == "__main__":
    unittest.main()
