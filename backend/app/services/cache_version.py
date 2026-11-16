"""结果缓存版本感知：缓存键版本、主动失效记录与查询结果数据集。

数据链：读取查询结果与可用的缓存策略，按查询结果版本与转换模板版本读写缓存，
生成查询结果数据集。缓存键必须由缓存键模板、查询结果版本、转换模板版本共同
生成，缺任一项都拒绝生成，避免不同字段口径落入同一缓存。

版本变化（含查询结果版本与转换模板版本同时变化）时，旧缓存明确失效并写入
缓存策略契约：查询入口从契约读取当前有效缓存版本与命中率，命中率与失效结果
可从查询结果数据集复核。事务写入失败时数据集整体回滚，不留半批数据。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "cache"
TABLE_QUERY_RESULT = "_cache_query_result"   # 查询结果（数据链原始数据）
TABLE_ENTRY = "_cache_entry"                 # 缓存条目
TABLE_INVALIDATION = "_cache_invalidation"   # 主动失效记录
TABLE_CONTRACT = "_cache_contract"           # 缓存策略契约
TABLE_DATASET = "_cache_dataset"             # 查询结果数据集

ENTRY_ACTIVE = "有效"
ENTRY_INVALID = "已失效"
HIT = "命中"
MISS = "未命中"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_no(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    """按前缀递增编号：INV-0001、RUN-0001。"""
    top = 0
    for row in rows:
        text = str(row.get(field) or "")
        if text.startswith(prefix):
            try:
                top = max(top, int(text[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{top + 1:04d}"


class CacheKeyError(ValueError):
    """缓存键三要素不齐或模板参数缺失时抛出。"""


# 查询结果种子（数据链原始数据）。QUER-ORDER 故意放两条数据范围不一致的记录，
# 用来演示"数据范围不一致时按优先级决定采用哪一版"。
QUERY_RESULT_SEED: list[dict[str, Any]] = [
    {
        "查询编号": "QUER-ORDER",
        "查询名称": "order_id 订单查询",
        "查询结果版本": "qv1",
        "数据范围": "2026-09 全量订单",
        "优先级": 10,
        "结果字段": ["order_id", "status"],
        "结果内容": {"order_id": "1001", "status": "已发货"},
        "生成时间": "2026-09-05 09:00:00",
    },
    {
        "查询编号": "QUER-ORDER",
        "查询名称": "order_id 订单查询",
        "查询结果版本": "qv1",
        "数据范围": "2026-09 部分订单",
        "优先级": 5,
        "结果字段": ["order_id", "status"],
        "结果内容": {"order_id": "1001", "status": "待发货"},
        "生成时间": "2026-09-05 08:00:00",
    },
    {
        "查询编号": "QUER-STOCK",
        "查询名称": "库存余量查询",
        "查询结果版本": "qv1",
        "数据范围": "2026-09 全量库存",
        "优先级": 10,
        "结果字段": ["sku", "stock"],
        "结果内容": {"sku": "SKU-1", "stock": 42},
        "生成时间": "2026-09-05 09:00:00",
    },
]

# 缓存策略契约种子：查询入口从这里读当前有效缓存版本与命中率。
CONTRACT_SEED: list[dict[str, Any]] = [
    {
        "策略编号": "CACH-0002",
        "关联查询": "QUER-ORDER",
        "当前查询结果版本": "qv1",
        "当前转换模板版本": "tv1",
        "累计命中": 0,
        "累计未命中": 0,
        "失效次数": 0,
        "最近失效编号": None,
        "更新时间": "2026-09-05 09:00:00",
    },
    {
        "策略编号": "CACH-0003",
        "关联查询": "QUER-STOCK",
        "当前查询结果版本": "qv1",
        "当前转换模板版本": "tv1",
        "累计命中": 0,
        "累计未命中": 0,
        "失效次数": 0,
        "最近失效编号": None,
        "更新时间": "2026-09-05 09:00:00",
    },
]


class CacheVersionService:
    """版本感知缓存的数据链、主动失效与契约规则。"""

    def __init__(self) -> None:
        self._ensure_seed()

    # ---------- 初始化 ----------

    def _ensure_seed(self) -> None:
        if store.rows(TABLE_CONTRACT):
            return
        for order, row in enumerate(QUERY_RESULT_SEED, start=1):
            store.rows(TABLE_QUERY_RESULT).append({"id": order, **row})
        for order, row in enumerate(CONTRACT_SEED, start=1):
            store.rows(TABLE_CONTRACT).append({"id": order, **row})

    # ---------- 缓存键 ----------

    @staticmethod
    def build_cache_key(
        key_template: str,
        query_version: str,
        template_version: str,
        params: dict[str, Any],
    ) -> str:
        """缓存键模板、查询结果版本、转换模板版本共同生成缓存键。

        三要素缺任一项都拒绝生成：少了版本，不同字段口径的结果会落入同一缓存。
        """
        missing = []
        if not str(key_template or "").strip():
            missing.append("缓存键模板")
        if not str(query_version or "").strip():
            missing.append("查询结果版本")
        if not str(template_version or "").strip():
            missing.append("转换模板版本")
        if missing:
            raise CacheKeyError(f"缓存键生成缺少：{'、'.join(missing)}")
        try:
            rendered = str(key_template).format(**params)
        except KeyError as exc:
            raise CacheKeyError(f"缓存键模板参数缺失：{exc.args[0]}") from exc
        return f"{rendered}::qv={query_version}::tv={template_version}"

    # ---------- 读取侧 ----------

    @staticmethod
    def _contract_view(row: dict[str, Any]) -> dict[str, Any]:
        total = int(row.get("累计命中", 0)) + int(row.get("累计未命中", 0))
        hit_rate = round(int(row.get("累计命中", 0)) / total, 4) if total else 0.0
        return {**row, "命中率": hit_rate}

    def list_contracts(self) -> list[dict[str, Any]]:
        """缓存策略契约列表：查询入口读取当前有效缓存版本与命中率。"""
        return [self._contract_view(row) for row in store.rows(TABLE_CONTRACT)]

    def list_queries(self) -> list[dict[str, Any]]:
        """可执行查询清单（供批量多选），按查询编号去重。"""
        seen: dict[str, dict[str, Any]] = {}
        for row in store.rows(TABLE_QUERY_RESULT):
            qid = str(row.get("查询编号"))
            seen.setdefault(qid, {"查询编号": qid, "查询名称": row.get("查询名称")})
        return list(seen.values())

    def list_query_results(self) -> list[dict[str, Any]]:
        return list(store.rows(TABLE_QUERY_RESULT))

    def list_entries(
        self,
        policy_no: str | None = None,
        query_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(TABLE_ENTRY)
        if policy_no:
            rows = [row for row in rows if str(row.get("策略编号")) == policy_no]
        if query_id:
            rows = [row for row in rows if str(row.get("查询编号")) == query_id]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return list(rows)

    def list_invalidations(self, policy_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(TABLE_INVALIDATION)
        if policy_no:
            rows = [row for row in rows if str(row.get("策略编号")) == policy_no]
        return list(rows)

    def dataset_review(
        self,
        policy_no: str | None = None,
        query_id: str | None = None,
        batch_no: str | None = None,
    ) -> dict[str, Any]:
        """复核入口：从查询结果数据集算命中率，并带出关联失效记录。"""
        rows = store.rows(TABLE_DATASET)
        if policy_no:
            rows = [row for row in rows if str(row.get("策略编号")) == policy_no]
        if query_id:
            rows = [row for row in rows if str(row.get("查询编号")) == query_id]
        if batch_no:
            rows = [row for row in rows if str(row.get("批次编号")) == batch_no]
        hits = sum(1 for row in rows if row.get("命中标识") == HIT)
        total = len(rows)
        summary = {
            "数据集条数": total,
            "命中": hits,
            "未命中": total - hits,
            "命中率": round(hits / total, 4) if total else 0.0,
        }
        invalidations = store.rows(TABLE_INVALIDATION)
        if policy_no:
            invalidations = [row for row in invalidations if str(row.get("策略编号")) == policy_no]
        if query_id:
            invalidations = [row for row in invalidations if str(row.get("查询编号")) == query_id]
        return {
            "items": list(rows),
            "total": total,
            "汇总": summary,
            "失效记录": list(invalidations),
        }

    # ---------- 数据链 ----------

    @staticmethod
    def _enabled_policies() -> dict[str, dict[str, Any]]:
        """可用的缓存策略：仅已启用状态参与数据链。"""
        return {
            str(row.get("策略编号")): row
            for row in store.rows(MODULE)
            if row.get("status") == "已启用"
        }

    def _pick_query_result(
        self, query_id: str, version: str
    ) -> tuple[dict[str, Any] | None, str]:
        """同一查询同一版本可能有多条数据范围不一致的记录：按优先级决定采用哪一版。"""
        candidates = [
            row
            for row in store.rows(TABLE_QUERY_RESULT)
            if str(row.get("查询编号")) == query_id
            and str(row.get("查询结果版本")) == version
        ]
        if not candidates:
            return None, ""
        candidates.sort(
            key=lambda row: (int(row.get("优先级", 0)), str(row.get("生成时间", ""))),
            reverse=True,
        )
        picked = candidates[0]
        if len(candidates) > 1:
            note = (
                f"数据范围不一致：候选 {len(candidates)} 版，"
                f"按优先级采用「{picked.get('数据范围')}」（优先级 {picked.get('优先级')}）"
            )
        else:
            note = "数据范围一致，无需优先级裁决"
        return picked, note

    @staticmethod
    def _find_active_entry(
        cache_key: str, staged: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        for row in store.rows(TABLE_ENTRY):
            if row.get("缓存键") == cache_key and row.get("状态") == ENTRY_ACTIVE:
                return row
        # 同批次内暂存的新条目也算命中，保证整组执行结果一致
        for row in staged:
            if row.get("缓存键") == cache_key:
                return row
        return None

    def execute_chain(
        self,
        query_ids: list[str],
        *,
        batch_label: str = "手动执行",
        simulate_write_failure: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """数据链整组执行：读查询结果与可用缓存策略 → 按版本读写缓存 → 生成数据集。

        事务语义：所有写入先暂存，任一查询失败即整体回滚，数据集不留半批记录。
        """
        query_ids = [str(q).strip() for q in query_ids if str(q or "").strip()]
        if not query_ids:
            return None, "未选择要执行的查询"
        policies = self._enabled_policies()
        contracts = {str(c.get("关联查询")): c for c in store.rows(TABLE_CONTRACT)}

        batch_no = _next_no(store.rows(TABLE_DATASET), "批次编号", "RUN-")
        staged_entries: list[dict[str, Any]] = []
        staged_dataset: list[dict[str, Any]] = []
        contract_delta: dict[str, dict[str, int]] = {}
        results: list[dict[str, Any]] = []

        for qid in query_ids:
            contract = contracts.get(qid)
            if contract is None:
                return None, f"查询 {qid} 未绑定缓存策略契约，批次 {batch_no} 已整体回滚"
            policy_no = str(contract.get("策略编号"))
            policy = policies.get(policy_no)
            if policy is None:
                return None, f"缓存策略 {policy_no} 未启用，批次 {batch_no} 已整体回滚"
            qv = str(contract.get("当前查询结果版本"))
            tv = str(contract.get("当前转换模板版本"))
            picked, note = self._pick_query_result(qid, qv)
            if picked is None:
                return None, f"查询 {qid} 在查询结果版本 {qv} 下没有原始数据，批次 {batch_no} 已整体回滚"
            params = dict(picked.get("结果内容") or {})
            try:
                cache_key = self.build_cache_key(str(policy.get("缓存键模板")), qv, tv, params)
            except CacheKeyError as exc:
                return None, f"{exc}，批次 {batch_no} 已整体回滚"
            hit_entry = self._find_active_entry(cache_key, staged_entries)
            if hit_entry is not None:
                hit_flag = HIT
                content = hit_entry.get("结果内容")
            else:
                hit_flag = MISS
                content = picked.get("结果内容")
                if simulate_write_failure or not content:
                    return None, (
                        f"查询 {qid} 缓存写入失败，事务已整体回滚："
                        f"批次 {batch_no} 未产生任何数据集记录"
                    )
                staged_entries.append({
                    "id": _next_id(store.rows(TABLE_ENTRY)) + len(staged_entries),
                    "缓存键": cache_key,
                    "策略编号": policy_no,
                    "查询编号": qid,
                    "查询结果版本": qv,
                    "转换模板版本": tv,
                    "数据范围": picked.get("数据范围"),
                    "结果内容": content,
                    "状态": ENTRY_ACTIVE,
                    "写入时间": _now(),
                    "失效时间": None,
                })
            staged_dataset.append({
                "id": _next_id(store.rows(TABLE_DATASET)) + len(staged_dataset),
                "批次编号": batch_no,
                "批次说明": batch_label,
                "策略编号": policy_no,
                "查询编号": qid,
                "缓存键": cache_key,
                "命中标识": hit_flag,
                "查询结果版本": qv,
                "转换模板版本": tv,
                "数据范围": picked.get("数据范围"),
                "优先级说明": note,
                "结果内容": content,
                "产生时间": _now(),
            })
            delta = contract_delta.setdefault(policy_no, {HIT: 0, MISS: 0})
            delta[hit_flag] += 1
            results.append({
                "查询编号": qid,
                "缓存键": cache_key,
                "命中标识": hit_flag,
                "查询结果版本": qv,
                "转换模板版本": tv,
                "数据范围": picked.get("数据范围"),
                "优先级说明": note,
                "结果内容": content,
            })

        # 事务提交：任一环节失败已在上方直接返回，走到这里才整体落库
        store.rows(TABLE_ENTRY).extend(staged_entries)
        store.rows(TABLE_DATASET).extend(staged_dataset)
        for policy_no, delta in contract_delta.items():
            for contract in store.rows(TABLE_CONTRACT):
                if str(contract.get("策略编号")) == policy_no:
                    contract["累计命中"] = int(contract.get("累计命中", 0)) + delta[HIT]
                    contract["累计未命中"] = int(contract.get("累计未命中", 0)) + delta[MISS]
                    contract["更新时间"] = _now()
        return {
            "批次编号": batch_no,
            "批次说明": batch_label,
            "结果列表": results,
            "写入缓存条数": len(staged_entries),
        }, f"{batch_label}完成：批次 {batch_no}，整组 {len(results)} 条查询，新写缓存 {len(staged_entries)} 条"

    def warmup(self, query_ids: list[str]) -> tuple[dict[str, Any] | None, str]:
        """缓存预热：批量多选的查询整组执行（同一批次），逐条返回命中结果。"""
        return self.execute_chain(query_ids, batch_label="缓存预热")

    # ---------- 版本发布（主动失效）----------

    def _query_name(self, query_id: str) -> str:
        for row in store.rows(TABLE_QUERY_RESULT):
            if str(row.get("查询编号")) == query_id:
                return str(row.get("查询名称") or query_id)
        return query_id

    def publish_version(
        self,
        policy_no: str,
        new_query_version: str,
        new_template_version: str,
        reason: str,
        new_result: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """发布新版本：旧缓存明确失效并写失效记录，缓存策略契约同步切换当前版本。

        查询结果版本与转换模板版本即使同时变化，旧版本组合的有效条目也全部
        显式失效，绝不让旧缓存在新版本下继续命中。
        """
        policy_no = str(policy_no or "").strip()
        new_query_version = str(new_query_version or "").strip()
        new_template_version = str(new_template_version or "").strip()
        reason = str(reason or "").strip() or "版本升级"
        if not policy_no or not new_query_version or not new_template_version:
            return None, "策略编号、查询结果版本、转换模板版本均为必填"
        contract = None
        for row in store.rows(TABLE_CONTRACT):
            if str(row.get("策略编号")) == policy_no:
                contract = row
                break
        if contract is None:
            return None, f"缓存策略 {policy_no} 未建立缓存策略契约"
        old_qv = str(contract.get("当前查询结果版本"))
        old_tv = str(contract.get("当前转换模板版本"))
        if (new_query_version, new_template_version) == (old_qv, old_tv):
            return None, f"版本组合 {new_query_version}+{new_template_version} 与当前一致，无需发布"

        # 1. 旧缓存明确失效：该策略下版本组合不等于新组合的有效条目全部失效
        invalidated: list[dict[str, Any]] = []
        for entry in store.rows(TABLE_ENTRY):
            if str(entry.get("策略编号")) != policy_no or entry.get("状态") != ENTRY_ACTIVE:
                continue
            combo = (str(entry.get("查询结果版本")), str(entry.get("转换模板版本")))
            if combo != (new_query_version, new_template_version):
                entry["状态"] = ENTRY_INVALID
                entry["失效时间"] = _now()
                invalidated.append(entry)

        # 2. 主动失效记录：写入失效表并联动缓存策略契约，供查询入口复核
        records: list[dict[str, Any]] = []
        existing = list(store.rows(TABLE_INVALIDATION))
        for entry in invalidated:
            record = {
                "id": _next_id(existing),
                "失效编号": _next_no(existing, "失效编号", "INV-"),
                "策略编号": policy_no,
                "查询编号": entry.get("查询编号"),
                "缓存键": entry.get("缓存键"),
                "旧查询结果版本": entry.get("查询结果版本"),
                "旧转换模板版本": entry.get("转换模板版本"),
                "新查询结果版本": new_query_version,
                "新转换模板版本": new_template_version,
                "失效原因": reason,
                "失效时间": _now(),
            }
            existing.append(record)
            records.append(record)
        store.rows(TABLE_INVALIDATION).extend(records)

        # 3. 登记新版本查询结果，作为数据链下一轮执行的原始数据；
        #    未提供时按当前版本复制一份，保证链路可继续执行
        query_id = str(contract.get("关联查询"))
        result_table = store.rows(TABLE_QUERY_RESULT)
        exists = any(
            str(r.get("查询编号")) == query_id and str(r.get("查询结果版本")) == new_query_version
            for r in result_table
        )
        if not exists:
            if new_result:
                row = {
                    "查询编号": query_id,
                    "查询名称": new_result.get("查询名称") or self._query_name(query_id),
                    "查询结果版本": new_query_version,
                    "数据范围": new_result.get("数据范围") or "新版本全量",
                    "优先级": int(new_result.get("优先级") or 10),
                    "结果字段": new_result.get("结果字段") or [],
                    "结果内容": new_result.get("结果内容") or {},
                    "生成时间": _now(),
                }
            else:
                picked, _ = self._pick_query_result(query_id, old_qv)
                row = {
                    "查询编号": query_id,
                    "查询名称": self._query_name(query_id),
                    "查询结果版本": new_query_version,
                    "数据范围": (picked or {}).get("数据范围") or "新版本全量",
                    "优先级": int((picked or {}).get("优先级") or 10),
                    "结果字段": list((picked or {}).get("结果字段") or []),
                    "结果内容": dict((picked or {}).get("结果内容") or {}),
                    "生成时间": _now(),
                }
            result_table.append({"id": _next_id(result_table), **row})

        # 4. 契约切换：查询入口读到的当前有效缓存版本随之更新
        contract["当前查询结果版本"] = new_query_version
        contract["当前转换模板版本"] = new_template_version
        contract["失效次数"] = int(contract.get("失效次数", 0)) + len(records)
        contract["最近失效编号"] = records[-1]["失效编号"] if records else contract.get("最近失效编号")
        contract["更新时间"] = _now()
        return {
            "契约": self._contract_view(contract),
            "失效记录": records,
            "旧版本组合": f"{old_qv}+{old_tv}",
            "新版本组合": f"{new_query_version}+{new_template_version}",
        }, (
            f"已发布 {policy_no} 新版本 {new_query_version}+{new_template_version}，"
            f"明确失效旧缓存 {len(records)} 条"
        )
