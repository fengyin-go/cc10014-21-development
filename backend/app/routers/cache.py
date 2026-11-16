"""结果缓存接口：维护缓存策略，覆盖提交校验、启用缓存、停用缓存等动作。

同时承载版本感知缓存的数据链接口：缓存键版本、主动失效记录、缓存策略契约、
缓存预热与查询结果数据集复核。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cache import CacheService
from app.services.cache_version import CacheVersionService

router = APIRouter(prefix="/api/cache", tags=["结果缓存"])

service = CacheService()
version_service = CacheVersionService()

LIST_FIELDS = ["策略编号", "关联服务", "缓存键模板", "过期秒数", "淘汰算法", "预热策略", "命中率阈值", "最大条目"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤结果缓存列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条缓存策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="缓存策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条缓存策略执行提交校验、启用缓存、停用缓存；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出结果缓存清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


# ---------- 版本感知缓存：缓存键版本、主动失效、契约与数据集复核 ----------


@router.get("/version/contracts")
def list_contracts() -> dict[str, Any]:
    """缓存策略契约：查询入口从这里读取当前有效缓存版本与命中率。"""
    return {"items": version_service.list_contracts()}


@router.get("/version/queries")
def list_queries() -> dict[str, Any]:
    """可执行查询清单：供缓存预热与数据链执行批量多选。"""
    return {"items": version_service.list_queries()}


@router.get("/version/query-results")
def list_query_results() -> dict[str, Any]:
    """查询结果原始数据：数据链读取的输入，含数据范围与优先级。"""
    return {"items": version_service.list_query_results()}


@router.get("/version/entries")
def list_cache_entries(
    policy_no: str | None = Query(default=None, description="按策略编号过滤"),
    query_id: str | None = Query(default=None, description="按查询编号过滤"),
    status: str | None = Query(default=None, description="有效、已失效"),
) -> dict[str, Any]:
    """缓存条目列表：每条都带查询结果版本与转换模板版本。"""
    return {"items": version_service.list_entries(policy_no, query_id, status)}


@router.get("/version/invalidations")
def list_invalidations(
    policy_no: str | None = Query(default=None, description="按策略编号过滤"),
) -> dict[str, Any]:
    """主动失效记录：旧缓存被明确失效的完整轨迹。"""
    return {"items": version_service.list_invalidations(policy_no)}


@router.get("/version/dataset")
def dataset_review(
    policy_no: str | None = Query(default=None, description="按策略编号过滤"),
    query_id: str | None = Query(default=None, description="按查询编号过滤"),
    batch_no: str | None = Query(default=None, description="按批次编号过滤"),
) -> dict[str, Any]:
    """查询结果数据集复核：命中率汇总与失效记录都可从这里核对。"""
    return version_service.dataset_review(policy_no, query_id, batch_no)


@router.post("/version/execute", response_model=ActionResult)
def execute_chain(payload: EntryPayload) -> ActionResult:
    """执行数据链：整组读取查询结果与可用缓存策略，按版本读写缓存并生成数据集。

    事务写入失败时整体回滚，数据集不留半批记录。
    """
    values = payload.values
    query_ids = values.get("查询编号列表") or []
    if isinstance(query_ids, str):
        query_ids = [query_ids]
    result, message = version_service.execute_chain(
        query_ids,
        batch_label=str(values.get("批次说明") or "手动执行"),
        simulate_write_failure=bool(values.get("模拟写入失败")),
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/version/warmup", response_model=ActionResult)
def warmup(payload: EntryPayload) -> ActionResult:
    """缓存预热：批量多选的查询整组执行，逐条返回命中结果。"""
    values = payload.values
    query_ids = values.get("查询编号列表") or []
    if isinstance(query_ids, str):
        query_ids = [query_ids]
    result, message = version_service.warmup(query_ids)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/version/publish", response_model=ActionResult)
def publish_version(payload: EntryPayload) -> ActionResult:
    """发布新版本：旧缓存明确失效并写失效记录，缓存策略契约同步切换当前版本。"""
    values = payload.values
    new_result = values.get("新查询结果")
    result, message = version_service.publish_version(
        str(values.get("策略编号") or ""),
        str(values.get("查询结果版本") or ""),
        str(values.get("转换模板版本") or ""),
        str(values.get("变更说明") or ""),
        new_result if isinstance(new_result, dict) else None,
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)
