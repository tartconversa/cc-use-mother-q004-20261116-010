from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
import json

from .service import LakeService


def dimensions(service: LakeService):
    service.register_source("box-feed", "smart_box")
    service.register_source("scale-feed", "scale")
    service.register_source("pay-feed", "payment")
    service.register_dimension({"kind": "street", "external_id": "street-1", "name": "合成街道",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "community", "external_id": "community-1", "name": "合成社区",
                                "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}})
    service.register_dimension({"kind": "device", "external_id": "box-1", "name": "合成回收箱",
                                "valid_from": "2026-01-01", "attributes": {"community_id": "community-1"}})
    service.register_dimension({"kind": "station", "external_id": "station-1", "name": "合成中转站",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "material", "external_id": "foam", "name": "泡沫",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "actor", "external_id": "collector-1", "name": "合成投递人",
                                "valid_from": "2026-01-01"})
    service.settlement.put_rate("foam", "smart_box", "2026-01-01", "0.08")


def events():
    base = {"happened_at": "2026-07-15T10:00:00+08:00", "batch_id": "batch-1"}
    return [
        {**base, "source": "box-feed", "external_id": "box-event-1", "event_type": "deposit",
         "channel": "smart_box", "material_id": "foam", "device_id": "box-1", "weight_kg": "10"},
        {**base, "source": "scale-feed", "external_id": "scale-event-1", "event_type": "weigh_in",
         "channel": "scale", "material_id": "foam", "station_id": "station-1", "weight_kg": "10"},
        {**base, "source": "pay-feed", "external_id": "pay-event-1", "event_type": "payout",
         "channel": "payment", "actor_id": "collector-1", "payout_yuan": "0.8"},
    ]


def glass_events(batch_id: str, tag: str, weight: str, payout: str, happened_at: str,
                 material_id: str = "glass"):
    return [
        {"source": "box-feed", "external_id": f"box-{tag}", "event_type": "deposit",
         "channel": "smart_box", "material_id": material_id, "device_id": "box-1",
         "weight_kg": weight, "happened_at": happened_at, "batch_id": batch_id},
        {"source": "scale-feed", "external_id": f"scale-{tag}", "event_type": "weigh_in",
         "channel": "scale", "material_id": material_id, "station_id": "station-1",
         "weight_kg": weight, "happened_at": happened_at, "batch_id": batch_id},
        {"source": "pay-feed", "external_id": f"pay-{tag}", "event_type": "payout",
         "channel": "payment", "actor_id": "collector-1", "payout_yuan": payout,
         "happened_at": happened_at, "batch_id": batch_id},
    ]


def classification_flow(service: LakeService):
    """玻璃拆分、泡沫不变、不确定历史记录保持待判的完整口径演练。"""
    from .model import LakeError
    service.register_dimension({"kind": "material", "external_id": "glass", "name": "玻璃",
                                "valid_from": "2026-01-01"})
    service.settlement.put_rate("glass", "smart_box", "2026-01-01", "0.04")
    g1 = [service.ingest(row) for row in glass_events("batch-g1", "g1", "10", "0.4",
                                                      "2026-07-16T10:00:00+08:00")]
    g2 = [service.ingest(row) for row in glass_events("batch-g2", "g2", "4", "0.16",
                                                      "2026-07-17T10:00:00+08:00")]
    if any(row["state"] != "normalized" for row in g1 + g2):
        raise RuntimeError("glass batches did not normalize")
    service.classification.create_version(
        "category", [{"category": "foam", "name": "泡沫"}, {"category": "glass", "name": "玻璃"}],
        note="初始分类")
    service.classification.put_mapping("category", 1, "foam", "foam", "2026-01-01",
                                       "泡沫品类一对一沿用")
    service.classification.put_mapping("category", 1, "glass", "glass", "2026-01-01",
                                       "玻璃品类一对一沿用")
    july_v1 = service.preview("2026-07", "category", 1)
    if not july_v1["ready"]:
        raise RuntimeError("july preview under scheme v1 not ready")
    release_v1 = service.publish("july-cat-v1", "2026-07", july_v1["fingerprint"],
                                 "category", 1)
    # 管理部门拆分玻璃：新物料与 v2 分类版本；历史玻璃给出两个候选映射，保持待判
    for material, name in (("glass-clear", "透明玻璃"), ("glass-color", "杂色玻璃")):
        service.register_dimension({"kind": "material", "external_id": material,
                                    "name": name, "valid_from": "2026-09-01"})
    service.settlement.put_rate("glass-clear", "smart_box", "2026-09-01", "0.04")
    service.classification.create_version(
        "category", [{"category": "foam", "name": "泡沫"},
                     {"category": "glass-clear", "name": "透明玻璃"},
                     {"category": "glass-color", "name": "杂色玻璃"}],
        note="玻璃拆分为透明与杂色", expected_version=1)
    service.classification.put_mapping("category", 2, "foam", "foam", "2026-01-01",
                                       "泡沫不变，继续一对一")
    service.classification.put_mapping("category", 2, "glass-clear", "glass-clear",
                                       "2026-09-01", "新投透明玻璃直接对应")
    service.classification.put_mapping("category", 2, "glass-color", "glass-color",
                                       "2026-09-01", "新投杂色玻璃直接对应")
    service.classification.put_mapping("category", 2, "glass", "glass-clear",
                                       "2026-01-01", "历史玻璃候选：透明（待裁定）",
                                       valid_to="2026-09-01")
    service.classification.put_mapping("category", 2, "glass", "glass-color",
                                       "2026-01-01", "历史玻璃候选：杂色（待裁定）",
                                       valid_to="2026-09-01")
    s1 = [service.ingest(row) for row in glass_events("batch-s1", "s1", "6", "0.24",
                                                      "2026-09-16T10:00:00+08:00",
                                                      material_id="glass-clear")]
    if any(row["state"] != "normalized" for row in s1):
        raise RuntimeError("september batch did not normalize")
    # 有抽检证据的 g1 逐事实裁定为透明玻璃；无证据的 g2 保持待判
    for row in g1:
        service.classification.put_decision("category", 2, row["fact_id"], "glass-clear",
                                            "2026-08 仓库抽检记录#7：该批为透明玻璃")
    batch_g1 = service.batch_status("batch-g1", "category", 2)
    if not batch_g1["classification"]["consistent"]:
        raise RuntimeError("adjudicated batch should be category-consistent")
    preview_v2 = service.preview("2026-07", "category", 2)
    pending_ids = preview_v2["mart"]["pending_fact_ids"]
    if sorted([g2[0]["fact_id"], g2[2]["fact_id"]]) != pending_ids:
        raise RuntimeError("uncertain historical facts must stay pending")
    release_v2 = service.publish("july-cat-v2", "2026-07", preview_v2["fingerprint"],
                                 "category", 2)
    # 跨期报表：当时口径与可比口径并存
    as_recorded = service.street_report("street-1", "2026-07", "2026-09")
    comparable = service.street_report("street-1", "2026-07", "2026-09", "category", 2)
    # 映射修订：泡沫映射依据更新为新规文号，旧预览指纹立即失效
    stale_fingerprint = service.preview("2026-07", "category", 2)["fingerprint"]
    service.classification.put_mapping("category", 2, "foam", "foam", "2026-01-01",
                                       "泡沫不变，依据《可回收物目录（2026修订）》第3条",
                                       expected_revision=1)
    try:
        service.publish("july-cat-v3", "2026-07", stale_fingerprint, "category", 2)
        raise RuntimeError("stale fingerprint must not publish")
    except LakeError as error:
        if error.code != "stale_preview":
            raise
    # 映射撤回：杂色候选撤回后，g2 唯一映射到透明玻璃；冻结版仍保留待判原貌
    mappings = service.classification.mappings("category", 2)
    color_rule = [row for row in mappings
                  if row["material_id"] == "glass" and row["category"] == "glass-color"][0]
    service.classification.withdraw_mapping("category", 2, color_rule["rule_id"],
                                            "复核后确认历史玻璃不含杂色", expected_revision=1)
    replay = [service.ingest(row) for row in glass_events("batch-g2", "g2", "4", "0.16",
                                                          "2026-07-17T10:00:00+08:00")]
    if any(row["created"] for row in replay):
        raise RuntimeError("replayed old batch must stay idempotent")
    frozen_v2 = service.releases.get(release_v2["release_id"])
    if frozen_v2["content"]["mart"]["pending_fact_ids"] != pending_ids:
        raise RuntimeError("frozen release must keep its original pending facts")
    preview_v3 = service.preview("2026-07", "category", 2)
    if preview_v3["mart"]["pending_fact_ids"]:
        raise RuntimeError("withdrawn candidate should resolve pending facts")
    release_v3 = service.publish("july-cat-v3", "2026-07", preview_v3["fingerprint"],
                                 "category", 2)
    explained = service.fact_classification(g2[0]["fact_id"], "category", 2)
    if explained["resolution"]["status"] != "mapped":
        raise RuntimeError("resolved fact should trace its mapping basis")
    diff = service.releases.compare(release_v2["release_id"], release_v3["release_id"])
    return {"split_release_ids": [release_v1["release_id"], release_v2["release_id"],
                                  release_v3["release_id"]],
            "pending_fact_ids": pending_ids,
            "batch_g1_consistent": batch_g1["classification"]["consistent"],
            "as_recorded_rows": len(as_recorded["rows"]),
            "comparable_rows": len(comparable["rows"]),
            "frozen_pending_kept": frozen_v2["content"]["mart"]["pending_fact_ids"] == pending_ids,
            "replay_idempotent": all(not row["created"] for row in replay),
            "resolution_ref": explained["resolution"]["ref"]["kind"],
            "v3_changes": len(diff["changes"])}


def run_demo():
    with TemporaryDirectory() as folder:
        service = LakeService(Path(folder) / "lake.sqlite3")
        dimensions(service)
        outcomes = [service.ingest(event) for event in events()]
        if any(row["state"] != "normalized" for row in outcomes):
            raise RuntimeError("synthetic events did not normalize")
        duplicate = service.ingest(events()[0])
        if duplicate["created"]:
            raise RuntimeError("duplicate event created another fact")
        checked = service.batch_status("batch-1")
        if not checked["ready"] or checked["deposit_kg"] != "10":
            raise RuntimeError("material balance failed")
        preview = service.preview("2026-07")
        if not preview["ready"] or preview["mart"]["rows"][0]["deposit_kg"] != "10":
            raise RuntimeError("mart preview failed")
        release = service.publish("july-final", "2026-07", preview["fingerprint"])
        frozen = service.releases.get(release["release_id"])
        if frozen["fingerprint"] != preview["fingerprint"]:
            raise RuntimeError("release fingerprint mismatch")
        lineage = service.lineage.fact(outcomes[0]["fact_id"])
        if lineage["fact"]["street_id"] != "street-1":
            raise RuntimeError("fact lineage lost effective dimension")
        classification = classification_flow(service)
        return {"result": "ok", "normalized_fact_ids": [row["fact_id"] for row in outcomes],
                "duplicate_created": duplicate["created"], "batch_ready": checked["ready"],
                "deposit_kg": checked["deposit_kg"], "release_id": release["release_id"],
                "release_fingerprint": frozen["fingerprint"],
                "classification": classification}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True))
