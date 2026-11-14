"""负载均衡策略接口：维护均衡策略，覆盖启用策略、切换策略、关闭策略等动作。

权限口径：只有策略负责人能执行动作，其他人只有只读权限（列表、明细、导出）。
请求可以通过请求头 `X-Operator` 或请求体 `operator` 声明操作人，
都没有时按平台默认值班人处理。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.balance import BalanceService

router = APIRouter(prefix="/api/balance", tags=["负载均衡策略"])

service = BalanceService()

LIST_FIELDS = ["策略编号", "策略名称", "均衡算法", "服务列表", "权重配置", "故障转移", "健康检查", "策略状态"]
STATUSES = ["草稿", "生效中", "已切换", "已关闭"]
PERMISSION_DENIED_STATUS = 403


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已切换、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤负载均衡策略列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出负载均衡策略清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "balance", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条均衡策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"均衡策略 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/preflight", response_model=dict)
def preflight(entry_id: int) -> dict:
    """切换前预检：校验服务列表与权重配置是否一一对应，返回流量分配预览。"""
    result = service.preflight(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"均衡策略 {entry_id} 不存在或已归档")
    return result


@router.get("/{entry_id}/failover", response_model=dict)
def get_failover(entry_id: int) -> dict:
    """故障转移详情：结论只引用当前有效服务列表，历史回执单独列出。"""
    result = service.get_failover(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"均衡策略 {entry_id} 不存在或已归档")
    return result


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult:
    """登记一条均衡策略，缺字段时说明原因而不是静默丢弃。"""
    values = payload.all_values
    entry, missing = service.create_entry(values, operator=x_operator)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="均衡策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_operator: str | None = Header(default=None, alias="X-Operator"),
) -> ActionResult | JSONResponse:
    """对单条均衡策略执行启用策略、切换策略、关闭策略。

    越权请求直接以 403 拒绝，且不修改服务列表与权重配置；
    切换前的配置校验失败时返回 ok=False，并回滚流量分配结果。
    """
    values = payload.all_values
    action = str(values.get("action") or "").strip()
    outcome = service.run_action(entry_id, action, operator=x_operator, values=values)
    body = ActionResult(**outcome.payload).model_dump()
    if outcome.code == "forbidden":
        return JSONResponse(status_code=PERMISSION_DENIED_STATUS, content=body)
    return ActionResult(**outcome.payload)
