"""负载均衡策略业务规则：状态流转、负责人权限、服务权重校验与故障转移回执。

这里集中三件事，路由层只负责转发：

1. 负责人权限：只有策略负责人可以执行启用 / 切换 / 关闭；其他人员只读。
2. 服务列表与权重配置一一对应：解析历史字符串格式，切换前统一校验。
3. 故障转移回执：切换成功时按「当前有效服务列表」生成一份回执，
   同一策略重复切换不会追加结论；并发切换由进程内锁串行化，只产生一个有效版本。
"""
from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import settings
from app.store import store

MODULE = "balance"
REQUIRED_FIELDS = ["策略编号", "策略名称", "均衡算法"]
STATUS_ORDER = ["草稿", "生效中", "已切换", "已关闭"]
ACTION_RULES = {"启用策略": "草稿", "切换策略": "生效中", "关闭策略": "已切换"}
NEGATIVE_ACTIONS = []
SWITCH_ACTION = "切换策略"
COMPLETED_STATUS = "已切换"
PENDING_STATUS = "生效中"

#: 切换时可以携带的配置字段：既接受中文业务字段，也接受结构化字段。
CONFIG_KEYS = ("服务列表", "权重配置", "services", "weights", "service_weights")

#: 进程内串行化所有写操作。FastAPI 的同步接口在线程池里执行，
#: 同一策略的两个并发切换会在这里排队，后到的一个会看到「已切换」而不再追加结论。
_LOCK = threading.RLock()

_SERVICE_SPLIT = re.compile(r"[,，;；、|\n]+")
_WEIGHT_ENTRY = re.compile(r"^(?P<service>.+?)\s*[:：=]\s*(?P<weight>-?\d+(?:\.\d+)?)$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass
class ActionOutcome:
    """动作结果：ok=False 表示请求被拒绝，业务状态保持原值。"""

    ok: bool
    message: str
    code: str | None = None
    entry: dict[str, Any] | None = None
    changed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "message": self.message,
            "code": self.code,
            "changed": self.changed,
            "entry": self.entry,
        }
        data.update(self.extra)
        return data


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def _resolve_operator(header_operator: str | None, values: dict[str, Any]) -> str:
    """请求身份优先级：请求头 X-Operator > 请求体 operator/操作人 > 平台默认值班人。

    中文姓名放请求头时浏览器/代理会按 latin-1 传输，这里先尝试还原成 UTF-8；
    前端实际使用请求体传操作人，请求头只作为兼容入口。
    """
    candidate = _normalize(_recover_text(header_operator))
    if candidate:
        return candidate
    for key in ("operator", "操作人", "当前操作人"):
        candidate = _normalize(values.get(key))
        if candidate:
            return candidate
    return settings.default_operator


def _recover_text(value: Any) -> str:
    text = _normalize(value)
    if not text:
        return ""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def policy_owner(row: dict[str, Any]) -> str:
    """历史数据没有负责人字段时，归平台默认值班人维护，保证只读人员始终无法越权。"""
    return _normalize(row.get("策略负责人")) or settings.default_operator


def _extract_services(raw: Any) -> list[str]:
    """把服务列表统一成有序字符串数组，兼容列表、JSON 与分隔符字符串。"""
    if raw is None:
        return []
    items: Any
    if isinstance(raw, dict):
        items = list(raw)
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        parsed: Any = None
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
        if isinstance(parsed, (list, tuple)):
            items = list(parsed)
        elif isinstance(parsed, dict):
            items = list(parsed)
        else:
            items = _SERVICE_SPLIT.split(text)

    services: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("服务") or item.get("service") or item.get("name")
        else:
            name = item
        name = _normalize(name)
        if name:
            services.append(name)
    return services


def _to_weight(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    text = _normalize(raw)
    if _NUMBER.match(text):
        number = float(text)
        return int(number) if number.is_integer() else None
    return None


def _extract_weights(raw: Any, services: list[str]) -> tuple[dict[str, int], list[str]]:
    """把权重配置解析成 {服务: 权重}，同时返回解析阶段的错误说明。

    兼容的写法：
    - {"订单查询服务": 50, "库存查询服务": 50}
    - [{"服务": "订单查询服务", "权重": 50}, ...] / [["订单查询服务", 50], ...]
    - "订单查询服务:50、库存查询服务:50" / "订单查询服务=50;库存查询服务=50"
    - [50, 50] 或 "50,50"（按服务列表顺序一一对应）
    """
    errors: list[str] = []
    if raw is None:
        return {}, errors

    entries: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}, errors
        if text.startswith("[") or text.startswith("{"):
            try:
                entries = json.loads(text)
            except (TypeError, ValueError):
                entries = _SERVICE_SPLIT.split(text)
        else:
            entries = _SERVICE_SPLIT.split(text)

    if isinstance(entries, dict):
        weights: dict[str, int] = {}
        for key, value in entries.items():
            weight = _to_weight(value)
            if weight is None:
                errors.append(f"服务「{key}」的权重「{value}」不是有效整数")
                continue
            weights[str(key).strip()] = weight
        return weights, errors

    if not isinstance(entries, (list, tuple)):
        weight = _to_weight(entries)
        if weight is None or len(services) != 1:
            errors.append(f"权重配置「{entries}」无法解析")
            return {}, errors
        return {services[0]: weight}, errors

    # 纯数字列表：按服务列表顺序对应。
    if entries and all(_to_weight(item) is not None for item in entries):
        if len(entries) != len(services):
            # 交给一一对应校验给出更准确的说明。
            numbers = [_to_weight(item) for item in entries]
            return {}, [f"权重配置包含 {len(numbers)} 个数值，无法与服务列表的 {len(services)} 个服务对应"]

    weights = {}
    for index, item in enumerate(entries):
        if isinstance(item, dict):
            name = _normalize(item.get("服务") or item.get("service") or item.get("name"))
            value = item.get("权重", item.get("weight"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            name = _normalize(item[0])
            value = item[1]
        elif isinstance(item, str):
            matched = _WEIGHT_ENTRY.match(item.strip())
            if not matched:
                errors.append(f"权重配置「{item}」缺少服务名或权重，格式应为「服务:权重」")
                continue
            name = _normalize(matched.group("service"))
            value = matched.group("weight")
        else:
            # 出现纯数字时按位置映射到服务列表。
            if index < len(services) and _to_weight(item) is not None:
                weights[services[index]] = _to_weight(item) or 0
                continue
            errors.append(f"权重配置第 {index + 1} 项「{item}」无法解析")
            continue

        weight = _to_weight(value)
        if not name:
            errors.append(f"权重配置第 {index + 1} 项缺少服务名")
            continue
        if weight is None:
            errors.append(f"服务「{name}」的权重「{value}」不是有效整数")
            continue
        if name in weights:
            errors.append(f"服务「{name}」的权重重复配置")
            continue
        weights[name] = weight
    return weights, errors


def _read_config(row: dict[str, Any]) -> tuple[list[str], dict[str, int], list[str]]:
    """读取策略当前生效的服务列表与权重配置，兼容历史存储格式。"""
    errors: list[str] = []

    if isinstance(row.get("services"), (list, tuple, set)):
        services = _extract_services(row.get("services"))
    elif isinstance(row.get("服务列表"), (list, tuple, set)):
        services = _extract_services(row.get("服务列表"))
    elif row.get("service_weights"):
        services = _extract_services(row.get("service_weights"))
    else:
        services = _extract_services(row.get("服务列表"))

    if isinstance(row.get("weights"), dict):
        weights, weight_errors = _extract_weights(row.get("weights"), services)
    elif row.get("service_weights"):
        weights, weight_errors = _extract_weights(row.get("service_weights"), services)
    else:
        weights, weight_errors = _extract_weights(row.get("权重配置"), services)

    errors.extend(weight_errors)
    return services, weights, errors


def _validate_config(
    services: list[str], weights: dict[str, int]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """校验服务列表与权重配置一一对应，返回 (错误列表, 流量分配)。"""
    errors: list[dict[str, str]] = []

    def add(code: str, message: str, service: str | None = None) -> None:
        item = {"code": code, "message": message}
        if service is not None:
            item["service"] = service
        errors.append(item)

    if not services:
        add("empty_services", "服务列表为空，至少需要登记一个服务")

    seen: set[str] = set()
    for service in services:
        if service in seen:
            add("duplicate_service", f"服务列表存在重复服务「{service}」", service)
        seen.add(service)

    if len(services) != len(weights):
        add(
            "weight_count_mismatch",
            f"服务列表 {len(services)} 个，权重配置 {len(weights)} 个，数量必须一一对应",
        )

    for service in services:
        if weights and service not in weights:
            add("weight_missing", f"服务「{service}」缺少权重配置", service)
    for service in weights:
        if service not in seen:
            add("weight_unknown_service", f"权重配置中的服务「{service}」不在服务列表内", service)

    for service, weight in weights.items():
        if weight < 0:
            add("weight_invalid", f"服务「{service}」的权重不能为负数", service)

    if weights and sum(weights.values()) <= 0:
        add("weight_zero_total", "权重配置总和必须大于 0")

    return errors, _allocation(services, weights) if not errors else []


def _allocation(services: list[str], weights: dict[str, int]) -> list[dict[str, Any]]:
    total = sum(weights.get(service, 0) for service in services)
    if total <= 0:
        return []
    allocation: list[dict[str, Any]] = []
    for service in services:
        weight = weights.get(service, 0)
        allocation.append(
            {
                "服务": service,
                "权重": weight,
                "流量占比": round(weight * 100 / total, 2),
            }
        )
    return allocation


def _format_weights(services: list[str], weights: dict[str, int]) -> str:
    ordered = [service for service in services if service in weights]
    ordered.extend(service for service in weights if service not in services)
    return "、".join(f"{service}:{weights[service]}" for service in ordered)


def _build_conclusion(
    policy_code: str, services: list[str], weights: dict[str, int]
) -> str:
    """故障转移结论只引用传入的当前有效服务列表。"""
    allocation = _allocation(services, weights)
    weight_text = "、".join(f"{service}:{weights.get(service, 0)}" for service in services)
    share_text = "、".join(
        f"{item['服务']} {item['流量占比']:.2f}%" for item in allocation
    )
    return (
        f"策略 {policy_code} 按当前有效服务列表分配流量："
        f"服务列表为 {'、'.join(services)}；"
        f"权重配置为 {weight_text}；"
        f"流量占比为 {share_text}；"
        f"任一服务故障时按列表顺序转移至下一个可用服务。"
    )


class BalanceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _LOCK:
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self._project(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with _LOCK:
            row = store.find(MODULE, entry_id)
            return self._project(row, detail=True) if row is not None else None

    def preflight(self, entry_id: int) -> dict[str, Any] | None:
        """切换前预检：只读地校验当前服务列表与权重配置是否一一对应。"""
        with _LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None
            services, weights, errors = _read_config(row)
            config_errors, allocation = _validate_config(services, weights)
            return {
                "id": row.get("id"),
                "策略编号": row.get("策略编号"),
                "策略负责人": policy_owner(row),
                "ok": not errors and not config_errors,
                "services": services,
                "weights": weights,
                "service_weights": self._service_weight_pairs(services, weights),
                "traffic_allocation": allocation,
                "errors": [{"code": "weight_parse_error", "message": item} for item in errors]
                + config_errors,
            }

    def get_failover(self, entry_id: int) -> dict[str, Any] | None:
        """故障转移详情：当前结论永远按当前有效服务列表重新给出。"""
        with _LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None
            services, weights, errors = _read_config(row)
            config_errors, allocation = _validate_config(services, weights)
            history = [copy.deepcopy(item) for item in (row.get("failover_history") or [])]
            latest = history[-1] if history else None
            policy_code = _normalize(row.get("策略编号")) or _normalize(row.get("策略名称"))
            valid = not errors and not config_errors
            conclusion = None
            if valid:
                conclusion = {
                    "ok": True,
                    "来源": "当前有效服务列表",
                    "策略编号": policy_code,
                    "服务列表": services,
                    "权重配置": {service: weights.get(service, 0) for service in services},
                    "流量分配": allocation,
                    "结论": _build_conclusion(policy_code, services, weights),
                }
            receipt_matches_current = bool(
                latest
                and latest.get("服务列表") == services
                and latest.get("权重配置") == {service: weights.get(service, 0) for service in services}
            )
            return {
                **self._project(row, detail=True),
                "policy": {
                    "id": row.get("id"),
                    "策略编号": row.get("策略编号"),
                    "策略名称": row.get("策略名称"),
                    "策略负责人": policy_owner(row),
                    "status": row.get("status"),
                    "version": int(row.get("version") or 1),
                },
                "conclusion": conclusion,
                "conclusion_errors": ([
                    {"code": "weight_parse_error", "message": item} for item in errors
                ] + config_errors),
                "receipt": latest,
                "receipt_matches_current": receipt_matches_current,
                "failover_history": history,
                "failover_count": len(history),
            }

    def create_entry(
        self, values: dict[str, Any], *, operator: str | None = None
    ) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        with _LOCK:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["策略负责人"] = (
                _normalize(values.get("策略负责人") or values.get("负责人"))
                or _resolve_operator(operator, values)
            )

            services = _extract_services(values.get("services") or values.get("服务列表"))
            weights, weight_errors = _extract_weights(
                values.get("weights")
                if values.get("weights") is not None
                else values.get("权重配置"),
                services,
            )
            if weight_errors:
                # 保留原始填写内容，切换前预检会提示具体不一致项。
                weights = {}

            entry["services"] = services
            entry["weights"] = weights
            entry["服务列表"] = "、".join(services) or _normalize(values.get("服务列表"))
            entry["权重配置"] = _format_weights(services, weights) or _normalize(values.get("权重配置"))
            entry["故障转移"] = _normalize(values.get("故障转移")) or "未生成"
            entry["健康检查"] = _normalize(values.get("健康检查")) or "开启"
            entry["策略状态"] = STATUS_ORDER[0]
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["version"] = 1
            entry["failover_history"] = []
            entry["updated_at"] = _now()
            rows.append(entry)
            return self._project(entry, detail=True), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str | None = None,
        values: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        values = dict(values or {})
        operator_name = _resolve_operator(operator, values)

        with _LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return ActionOutcome(
                    ok=False,
                    message=f"均衡策略 {entry_id} 不存在或已归档",
                    code="not_found",
                )
            if action not in ACTION_RULES:
                return ActionOutcome(
                    ok=False,
                    message=f"动作「{action}」不属于负载均衡策略可执行范围",
                    code="unknown_action",
                    entry=self._project(row, detail=True),
                )
            if not self._is_owner(row, operator_name):
                # 越权请求在业务层直接拒绝：不解析配置、不改写任何字段。
                return ActionOutcome(
                    ok=False,
                    message=(
                        f"仅策略负责人「{policy_owner(row)}」可执行「{action}」，"
                        f"当前操作人「{operator_name}」只有只读权限"
                    ),
                    code="forbidden",
                    entry=self._project(row, detail=True),
                )

            if action == SWITCH_ACTION:
                return self._switch(row, operator_name, values)

            backup = copy.deepcopy(row)
            try:
                target = ACTION_RULES[action]
                if target not in STATUS_ORDER:
                    return ActionOutcome(
                        ok=False,
                        message=f"目标状态「{target}」不在允许的状态序列里",
                        code="invalid_target_status",
                        entry=self._project(row, detail=True),
                    )
                row["status"] = target
                row["策略状态"] = target
                row["pending"] = target != STATUS_ORDER[-1]
                row["abnormal"] = action in NEGATIVE_ACTIONS
                row["updated_at"] = _now()
            except Exception:
                row.clear()
                row.update(backup)
                return ActionOutcome(
                    ok=False,
                    message=f"「{action}」执行失败，策略状态已回滚",
                    code="action_failed",
                    entry=self._project(row, detail=True),
                )
            return ActionOutcome(
                ok=True,
                message=f"均衡策略已{action}",
                entry=self._project(row, detail=True),
                changed=True,
            )

    # ------------------------------------------------------------------ 内部

    def _switch(
        self, row: dict[str, Any], operator: str, values: dict[str, Any]
    ) -> ActionOutcome:
        current_services, current_weights, _ = _read_config(row)
        submitted_services, submitted_weights, submitted_errors = self._submitted_config(
            values, current_services
        )

        if row.get("status") == COMPLETED_STATUS:
            if any(key in values and values.get(key) not in (None, "") for key in CONFIG_KEYS):
                return ActionOutcome(
                    ok=False,
                    message=(
                        "均衡策略已完成切换，重复切换不会追加故障转移结论；"
                        "如需调整服务列表或权重配置，请登记新的策略版本"
                    ),
                    code="policy_completed",
                    entry=self._project(row, detail=True),
                )
            return ActionOutcome(
                ok=True,
                message="均衡策略已处于已切换状态，本次重复切换未追加故障转移结论",
                code="already_switched",
                entry=self._project(row, detail=True),
                changed=False,
                extra={
                    "version": int(row.get("version") or 1),
                    "failover": (row.get("failover_history") or [None])[-1],
                    "failover_count": len(row.get("failover_history") or []),
                },
            )

        services = submitted_services if submitted_services is not None else current_services
        weights = submitted_weights if submitted_weights is not None else dict(current_weights)
        config_errors, allocation = _validate_config(services, weights)
        errors = [
            {"code": "weight_parse_error", "message": item} for item in submitted_errors
        ] + config_errors

        if errors:
            # 回滚：校验在写入之前完成，流量分配与两侧配置保持原值。
            before_services, before_weights, _ = _read_config(row)
            return ActionOutcome(
                ok=False,
                message=f"切换失败：{errors[0]['message']}",
                code=errors[0]["code"],
                entry=self._project(row, detail=True),
                changed=False,
                extra={
                    "rolled_back": True,
                    "errors": errors,
                    "traffic_allocation": _allocation(before_services, before_weights),
                    "services": before_services,
                    "weights": before_weights,
                },
            )

        backup = copy.deepcopy(row)
        try:
            row["services"] = list(services)
            row["weights"] = {service: weights[service] for service in services}
            row["服务列表"] = "、".join(services)
            row["权重配置"] = _format_weights(services, weights)
            row["service_weights"] = [
                {"服务": service, "权重": weights[service]} for service in services
            ]

            version = int(row.get("version") or 1) + 1
            history = list(row.get("failover_history") or [])
            receipt = {
                "id": f"{_normalize(row.get('策略编号'))}-FO-{len(history) + 1:03d}",
                "序号": len(history) + 1,
                "版本": version,
                "策略编号": row.get("策略编号"),
                "策略名称": row.get("策略名称"),
                "触发动作": SWITCH_ACTION,
                "操作人": operator,
                "生成时间": _now(),
                "服务列表": list(services),
                "权重配置": {service: weights[service] for service in services},
                "流量分配": allocation,
                "结论": _build_conclusion(
                    _normalize(row.get("策略编号")) or _normalize(row.get("策略名称")),
                    services,
                    weights,
                ),
            }
            row["failover_history"] = history + [receipt]
            row["故障转移"] = receipt["结论"]
            row["version"] = version
            row["status"] = COMPLETED_STATUS
            row["策略状态"] = COMPLETED_STATUS
            row["pending"] = False
            row["abnormal"] = False
            row["updated_at"] = _now()
        except Exception:
            row.clear()
            row.update(backup)
            return ActionOutcome(
                ok=False,
                message="切换失败：写入过程异常，服务列表、权重配置与流量分配已回滚",
                code="switch_failed",
                entry=self._project(row, detail=True),
                extra={"rolled_back": True},
            )

        return ActionOutcome(
            ok=True,
            message=f"均衡策略已切换，故障转移结论按当前有效服务列表生成（版本 v{row['version']}）",
            code="switched",
            entry=self._project(row, detail=True),
            changed=True,
            extra={
                "version": row["version"],
                "failover": receipt,
                "failover_count": len(row["failover_history"]),
                "traffic_allocation": allocation,
            },
        )

    def _submitted_config(
        self, values: dict[str, Any], services_hint: list[str]
    ) -> tuple[list[str] | None, dict[str, int] | None, list[str]]:
        """提取本次切换携带的配置；没有携带时返回 None，沿用当前生效配置。"""
        submitted_services: list[str] | None = None
        submitted_weights: dict[str, int] | None = None
        errors: list[str] = []

        pairs = values.get("service_weights")
        if pairs not in (None, "", []):
            pair_services = _extract_services(pairs)
            pair_weights, pair_errors = _extract_weights(pairs, pair_services)
            errors.extend(pair_errors)
            submitted_services = pair_services
            submitted_weights = pair_weights

        if "services" in values and values.get("services") not in (None, "", []):
            submitted_services = _extract_services(values.get("services"))
        elif "服务列表" in values and values.get("服务列表") not in (None, "", []):
            submitted_services = _extract_services(values.get("服务列表"))

        raw_weights = values.get("weights")
        if raw_weights is None:
            raw_weights = values.get("权重配置")
        if raw_weights not in (None, "", []):
            weights_hint = submitted_services if submitted_services is not None else services_hint
            submitted_weights, weight_errors = _extract_weights(raw_weights, weights_hint)
            errors.extend(weight_errors)

        return submitted_services, submitted_weights, errors

    def _is_owner(self, row: dict[str, Any], operator: str) -> bool:
        return bool(operator) and policy_owner(row) == operator

    def _service_weight_pairs(
        self, services: list[str], weights: dict[str, int]
    ) -> list[dict[str, Any]]:
        pairs = [
            {"服务": service, "权重": weights.get(service)}
            for service in services
        ]
        pairs.extend(
            {"服务": service, "权重": weight}
            for service, weight in weights.items()
            if service not in services
        )
        return pairs

    def _project(self, row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
        services, weights, parse_errors = _read_config(row)
        config_errors, allocation = _validate_config(services, weights)
        history = [copy.deepcopy(item) for item in (row.get("failover_history") or [])]
        latest = history[-1] if history else None
        policy_code = _normalize(row.get("策略编号")) or _normalize(row.get("策略名称"))

        valid = not parse_errors and not config_errors
        conclusion_text = (
            _build_conclusion(policy_code, services, weights)
            if valid
            else (latest or {}).get("结论") or _normalize(row.get("故障转移")) or "未生成"
        )

        data: dict[str, Any] = {
            "id": row.get("id"),
            "status": row.get("status"),
            "pending": bool(row.get("pending")),
            "abnormal": bool(row.get("abnormal")),
            "策略编号": row.get("策略编号"),
            "策略名称": row.get("策略名称"),
            "均衡算法": row.get("均衡算法"),
            "策略负责人": policy_owner(row),
            "服务列表": "、".join(services) or _normalize(row.get("服务列表")),
            "权重配置": _format_weights(services, weights) or _normalize(row.get("权重配置")),
            "故障转移": conclusion_text,
            "健康检查": row.get("健康检查"),
            "策略状态": row.get("status"),
            "version": int(row.get("version") or 1),
            "updated_at": row.get("updated_at"),
            "services": services,
            "weights": weights,
            "service_weights": self._service_weight_pairs(services, weights),
            "traffic_allocation": allocation,
            "config_ok": valid,
            "config_errors": [
                {"code": "weight_parse_error", "message": item} for item in parse_errors
            ] + config_errors,
            "failover": latest,
            "failover_count": len(history),
        }
        if detail:
            data["failover_history"] = history
        return data
