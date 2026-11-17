import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from looplake.api import create_app
from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService

JULY = "2026-07"
SEPT = "2026-09"


def seed_classification(service: LakeService):
    """v1：泡沫/玻璃并存；v2：玻璃拆分为透明与杂色，历史玻璃给两个候选映射。"""
    service.classification.create_version(
        "category", [{"category": "foam", "name": "泡沫"}, {"category": "glass", "name": "玻璃"}],
        note="初始分类")
    service.classification.put_mapping("category", 1, "foam", "foam", "2026-01-01", "泡沫沿用")
    service.classification.put_mapping("category", 1, "glass", "glass", "2026-01-01", "玻璃沿用")
    for material, name in (("glass-clear", "透明玻璃"), ("glass-color", "杂色玻璃")):
        service.register_dimension({"kind": "material", "external_id": material,
                                    "name": name, "valid_from": "2026-09-01"})
    service.settlement.put_rate("glass-clear", "smart_box", "2026-09-01", "0.04")
    service.classification.create_version(
        "category", [{"category": "foam", "name": "泡沫"},
                     {"category": "glass-clear", "name": "透明玻璃"},
                     {"category": "glass-color", "name": "杂色玻璃"}],
        note="玻璃拆分", expected_version=1)
    service.classification.put_mapping("category", 2, "foam", "foam", "2026-01-01", "泡沫不变")
    service.classification.put_mapping("category", 2, "glass-clear", "glass-clear",
                                       "2026-09-01", "新投透明玻璃直接对应")
    service.classification.put_mapping("category", 2, "glass-color", "glass-color",
                                       "2026-09-01", "新投杂色玻璃直接对应")
    service.classification.put_mapping("category", 2, "glass", "glass-clear",
                                       "2026-01-01", "历史玻璃候选：透明", valid_to="2026-09-01")
    return service.classification.put_mapping("category", 2, "glass", "glass-color",
                                              "2026-01-01", "历史玻璃候选：杂色",
                                              valid_to="2026-09-01")


def glass_batch(batch_id, prefix, weight="10", payout="0.4", happened_at="2026-07-16T10:00:00+08:00",
                material_id="glass"):
    return balanced_batch(batch_id, prefix, material_id=material_id, weight=weight,
                          payout=payout, happened_at=happened_at)


class SchemeVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def test_create_versions_and_audit(self):
        first = self.service.classification.create_version(
            "category", [{"category": "foam", "name": "泡沫"}], note="首版")
        self.assertEqual(first["version"], 1)
        second = self.service.classification.create_version(
            "category", [{"category": "foam", "name": "泡沫"},
                         {"category": "glass", "name": "玻璃"}], expected_version=1)
        self.assertEqual(second["version"], 2)
        versions = self.service.classification.versions("category")
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertEqual(versions[1]["categories"][1]["category"], "glass")
        actions = [row["action"] for row in self.service.store.audits()]
        self.assertEqual(actions.count("classification_version_added"), 2)

    def test_stale_expected_version(self):
        with self.assertRaises(LakeError) as failure:
            self.service.classification.create_version(
                "category", [{"category": "foam", "name": "泡沫"}], expected_version=3)
        self.assertEqual(failure.exception.code, "stale_scheme")

    def test_invalid_categories_rejected(self):
        for categories in ([], [{"category": "foam", "name": "泡沫"},
                                {"category": "foam", "name": "重复"}], ["x"], None):
            with self.subTest(categories=categories), self.assertRaises(LakeError):
                self.service.classification.create_version("category", categories)

    def test_withdraw_version_blocks_writes_not_reads(self):
        self.service.classification.create_version(
            "category", [{"category": "foam", "name": "泡沫"}])
        result = self.service.classification.withdraw_version("category", 1, "口径作废")
        self.assertEqual(result["status"], "withdrawn")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                    "2026-01-01", "x")
        self.assertEqual(failure.exception.code, "scheme_withdrawn")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.withdraw_version("category", 1, "再次撤回")
        self.assertEqual(failure.exception.code, "scheme_withdrawn")
        snap = self.service.classification.snapshot("category", 1)
        self.assertEqual(snap["status"], "withdrawn")

    def test_unknown_scheme_version(self):
        with self.assertRaises(LakeError) as failure:
            self.service.classification.snapshot("category", 9)
        self.assertEqual(failure.exception.code, "unknown_scheme_version")


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        self.service.classification.create_version(
            "category", [{"category": "foam", "name": "泡沫"},
                         {"category": "glass-clear", "name": "透明玻璃"},
                         {"category": "glass-color", "name": "杂色玻璃"}])

    def test_put_revise_and_idempotent(self):
        first = self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                        "2026-01-01", "依据一")
        self.assertEqual(first["revision"], 1)
        again = self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                        "2026-01-01", "依据一")
        self.assertFalse(again["created"])
        revised = self.service.classification.put_mapping(
            "category", 1, "foam", "foam", "2026-01-01", "依据二", expected_revision=1)
        self.assertEqual(revised["revision"], 2)
        history = self.service.classification.mappings("category", 1)
        self.assertEqual([row["revision"] for row in history], [1, 2])
        self.assertEqual(history[0]["basis"], "依据一")

    def test_stale_revision_guard(self):
        self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                "2026-01-01", "依据一")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                    "2026-01-01", "依据二", expected_revision=5)
        self.assertEqual(failure.exception.code, "stale_mapping")

    def test_same_category_overlap_rejected(self):
        self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                "2026-01-01", "甲", valid_to="2026-07-01")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                    "2026-06-01", "乙")
        self.assertEqual(failure.exception.code, "mapping_overlap")
        ok = self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                     "2026-07-01", "乙")
        self.assertTrue(ok["created"])

    def test_cross_category_overlap_allowed_as_ambiguity(self):
        self.service.classification.put_mapping("category", 1, "glass", "glass-clear",
                                                "2026-01-01", "候选透明")
        other = self.service.classification.put_mapping("category", 1, "glass", "glass-color",
                                                        "2026-01-01", "候选杂色")
        self.assertTrue(other["created"])

    def test_unknown_category_material_scheme(self):
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "foam", "unknown",
                                                    "2026-01-01", "x")
        self.assertEqual(failure.exception.code, "unknown_category")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "unknown", "foam",
                                                    "2026-01-01", "x")
        self.assertEqual(failure.exception.code, "unknown_material")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 9, "foam", "foam",
                                                    "2026-01-01", "x")
        self.assertEqual(failure.exception.code, "unknown_scheme_version")

    def test_invalid_range_rejected(self):
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                    "2026-01-01", "x", valid_to="2026-01-01")
        self.assertEqual(failure.exception.code, "invalid_range")

    def test_withdraw_and_reactivate(self):
        rule = self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                       "2026-01-01", "依据")
        done = self.service.classification.withdraw_mapping(
            "category", 1, rule["rule_id"], "撤回原因", expected_revision=1)
        self.assertEqual(done["status"], "withdrawn")
        snap = self.service.classification.snapshot("category", 1)
        self.assertEqual(snap["rules"], [])
        with self.assertRaises(LakeError) as failure:
            self.service.classification.withdraw_mapping("category", 1, rule["rule_id"], "再次")
        self.assertEqual(failure.exception.code, "mapping_withdrawn")
        back = self.service.classification.put_mapping("category", 1, "foam", "foam",
                                                       "2026-01-01", "恢复依据")
        self.assertEqual(back["revision"], 3)
        with self.assertRaises(LakeError) as failure:
            self.service.classification.withdraw_mapping("category", 1, 999, "无此规则")
        self.assertEqual(failure.exception.code, "unknown_mapping")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        self.color_rule = seed_classification(self.service)

    def ingest(self, rows):
        return [self.service.ingest(row) for row in rows]

    def test_foam_maps_unchanged_glass_stays_pending(self):
        self.ingest(balanced_batch("batch-foam", "f", material_id="foam", payout="0.8"))
        glass = self.ingest(glass_batch("batch-glass", "g"))
        report = self.service.month_report(JULY, "category", 2)
        self.assertEqual(report["caliber"]["scheme_version"], 2)
        rows = {row["category"]: row for row in report["rows"]}
        self.assertEqual(rows["foam"]["deposit_kg"], "10")
        self.assertNotIn("glass", rows)
        pending = {item["fact_id"]: item for item in report["pending"]}
        self.assertIn(glass[0]["fact_id"], pending)
        self.assertEqual(pending[glass[0]["fact_id"]]["reason"], "ambiguous")
        self.assertEqual({c["category"] for c in pending[glass[0]["fact_id"]]["candidates"]},
                         {"glass-clear", "glass-color"})
        self.assertTrue(all(c["basis"] for c in pending[glass[0]["fact_id"]]["candidates"]))
        self.assertIn(glass[2]["fact_id"], pending)

    def test_unmapped_material_stays_pending(self):
        self.service.register_dimension({"kind": "material", "external_id": "metal",
                                         "name": "金属", "valid_from": "2026-01-01"})
        metal = self.ingest(balanced_batch("batch-metal", "m", material_id="metal", payout=None))
        report = self.service.month_report(JULY, "category", 2)
        pending = {item["fact_id"]: item for item in report["pending"]}
        self.assertEqual(pending[metal[0]["fact_id"]]["reason"], "unmapped")
        self.assertEqual(pending[metal[0]["fact_id"]]["candidates"], [])

    def test_new_material_maps_directly_after_split(self):
        self.ingest(glass_batch("batch-sept", "s", weight="6", payout="0.24",
                                happened_at="2026-09-16T10:00:00+08:00",
                                material_id="glass-clear"))
        report = self.service.month_report(SEPT, "category", 2)
        self.assertEqual(report["rows"][0]["category"], "glass-clear")
        self.assertEqual(report["rows"][0]["deposit_kg"], "6")
        self.assertEqual(report["pending"], [])
        ref = report["rows"][0]["mapping_refs"][0]
        self.assertEqual(ref["kind"], "mapping")
        self.assertEqual(ref["basis"], "新投透明玻璃直接对应")

    def test_decision_resolves_then_withdraw_returns_pending(self):
        glass = self.ingest(glass_batch("batch-glass", "g"))
        fact_id = glass[0]["fact_id"]
        decision = self.service.classification.put_decision("category", 2, fact_id,
                                                            "glass-clear", "抽检记录#7")
        self.assertTrue(decision["created"])
        report = self.service.month_report(JULY, "category", 2)
        self.assertEqual(report["rows"][0]["category"], "glass-clear")
        ref = [r for r in report["rows"][0]["mapping_refs"] if r["kind"] == "decision"][0]
        self.assertEqual(ref["basis"], "抽检记录#7")
        again = self.service.classification.put_decision("category", 2, fact_id,
                                                         "glass-clear", "抽检记录#7")
        self.assertFalse(again["created"])
        self.service.classification.put_decision("category", 2, fact_id, "glass-color",
                                                 "复核改判", expected_revision=1)
        changed = self.service.month_report(JULY, "category", 2)
        self.assertEqual(changed["rows"][0]["category"], "glass-color")
        self.service.classification.withdraw_decision("category", 2, fact_id, "证据不足",
                                                      expected_revision=2)
        reverted = self.service.month_report(JULY, "category", 2)
        self.assertEqual(reverted["rows"], [])
        self.assertEqual(reverted["pending"][0]["reason"], "ambiguous")

    def test_decision_validation(self):
        glass = self.ingest(glass_batch("batch-glass", "g"))
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_decision("category", 2, 99999, "glass-clear", "x")
        self.assertEqual(failure.exception.code, "unknown_fact")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.put_decision("category", 2, glass[0]["fact_id"],
                                                     "unknown", "x")
        self.assertEqual(failure.exception.code, "unknown_category")
        with self.assertRaises(LakeError) as failure:
            self.service.classification.withdraw_decision("category", 2, glass[0]["fact_id"], "x")
        self.assertEqual(failure.exception.code, "unknown_decision")

    def test_payout_decision_resolves_inherited_material(self):
        glass = self.ingest(glass_batch("batch-glass", "g"))
        payout_fact = glass[2]["fact_id"]
        self.service.classification.put_decision("category", 2, payout_fact, "glass-clear",
                                                 "兑付单备注为透明玻璃")
        report = self.service.month_report(JULY, "category", 2)
        row = report["rows"][0]
        self.assertEqual(row["category"], "glass-clear")
        self.assertEqual(row["payout_yuan"], "0.4")
        self.assertEqual(row["deposit_kg"], "0")
        still_pending = [item["fact_id"] for item in report["pending"]]
        self.assertIn(glass[0]["fact_id"], still_pending)

    def test_batch_classification_consistency(self):
        glass = self.ingest(glass_batch("batch-glass", "g"))
        before = self.service.batch_status("batch-glass", "category", 2)
        self.assertFalse(before["classification"]["consistent"])
        weight_facts = [row["fact_id"] for row in glass[:2]]
        self.assertEqual(before["classification"]["pending_fact_ids"], weight_facts)
        for row in glass:
            self.service.classification.put_decision("category", 2, row["fact_id"],
                                                     "glass-clear", "抽检记录#7")
        after = self.service.batch_status("batch-glass", "category", 2)
        self.assertTrue(after["classification"]["consistent"])
        self.assertEqual(after["classification"]["categories"], ["glass-clear"])
        self.assertTrue(after["ready"])

    def test_explain_traces_basis(self):
        glass = self.ingest(glass_batch("batch-glass", "g"))
        explained = self.service.fact_classification(glass[0]["fact_id"], "category", 2)
        self.assertEqual(explained["resolution"]["reason"], "ambiguous")
        self.assertEqual(len(explained["resolution"]["candidates"]), 2)
        payout = self.service.fact_classification(glass[2]["fact_id"], "category", 2)
        self.assertEqual(payout["inherited"], {"street_id": "street-1", "material_id": "glass"})
        self.service.classification.put_decision("category", 2, glass[0]["fact_id"],
                                                 "glass-clear", "抽检记录#7")
        resolved = self.service.fact_classification(glass[0]["fact_id"], "category", 2)
        self.assertEqual(resolved["resolution"]["ref"]["kind"], "decision")
        self.assertEqual(len(resolved["decision_history"]), 1)


class CaliberReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        self.color_rule = seed_classification(self.service)
        self.ingest = lambda rows: [self.service.ingest(row) for row in rows]
        self.glass = self.ingest(glass_batch("batch-g1", "g1"))
        self.ingest(balanced_batch("batch-foam", "f", material_id="foam", payout="0.8"))
        self.ingest(glass_batch("batch-s1", "s1", weight="6", payout="0.24",
                                happened_at="2026-09-16T10:00:00+08:00",
                                material_id="glass-clear"))

    def test_as_recorded_report_unchanged(self):
        report = self.service.month_report(JULY)
        self.assertEqual(report["caliber"], {"mode": "as_recorded"})
        self.assertNotIn("pending", report)
        materials = {row["material_id"] for row in report["rows"]}
        self.assertEqual(materials, {"glass", "foam"})
        self.assertNotIn("mapping_refs", report["rows"][0])

    def test_cross_period_report_both_calibers(self):
        for row in self.glass:
            self.service.classification.put_decision("category", 2, row["fact_id"],
                                                     "glass-clear", "抽检记录#7")
        recorded = self.service.street_report("street-1", JULY, SEPT)
        by_month = {}
        for row in recorded["rows"]:
            by_month.setdefault(row["month"], {})[row["material_id"]] = row["deposit_kg"]
        self.assertEqual(by_month[JULY]["glass"], "10")
        self.assertEqual(by_month[SEPT]["glass-clear"], "6")
        comparable = self.service.street_report("street-1", JULY, SEPT, "category", 2)
        self.assertEqual(comparable["caliber"]["scheme_version"], 2)
        by_month = {}
        for row in comparable["rows"]:
            by_month.setdefault(row["month"], {})[row["category"]] = row["deposit_kg"]
        self.assertEqual(by_month[JULY]["glass-clear"], "10")
        self.assertEqual(by_month[SEPT]["glass-clear"], "6")
        self.assertEqual(comparable["pending"], [])

    def test_caliber_requires_scheme_and_version(self):
        with self.assertRaises(LakeError) as failure:
            self.service.month_report(JULY, "category")
        self.assertEqual(failure.exception.code, "invalid_caliber")
        with self.assertRaises(LakeError) as failure:
            self.service.month_report(JULY, scheme_version=2)
        self.assertEqual(failure.exception.code, "invalid_caliber")


class CaliberReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        self.color_rule = seed_classification(self.service)
        self.ingest = lambda rows: [self.service.ingest(row) for row in rows]
        self.glass = self.ingest(glass_batch("batch-g1", "g1"))
        self.ingest(balanced_batch("batch-foam", "f", material_id="foam", payout="0.8"))

    def test_publish_freezes_caliber_and_basis(self):
        preview = self.service.preview(JULY, "category", 2)
        self.assertEqual(preview["caliber"]["scheme_version"], 2)
        release = self.service.publish("july-v2", JULY, preview["fingerprint"],
                                       "category", 2)
        frozen = self.service.releases.get(release["release_id"])
        self.assertEqual(frozen["content"]["caliber"]["scheme"], "category")
        foam_row = [row for row in frozen["content"]["mart"]["rows"]
                    if row["category"] == "foam"][0]
        self.assertEqual(foam_row["mapping_refs"][0]["basis"], "泡沫不变")
        self.assertEqual(frozen["content"]["mart"]["pending_fact_ids"],
                         sorted([self.glass[0]["fact_id"], self.glass[2]["fact_id"]]))
        batches = frozen["content"]["reconciliation"]["batches"]
        glass_batch = [b for b in batches if b["batch_id"] == "batch-g1"][0]
        self.assertFalse(glass_batch["classification"]["consistent"])

    def test_mapping_revision_invalidates_preview(self):
        fingerprint = self.service.preview(JULY, "category", 2)["fingerprint"]
        self.service.classification.put_mapping("category", 2, "foam", "foam", "2026-01-01",
                                                "泡沫不变（新规文号）", expected_revision=1)
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-v2", JULY, fingerprint, "category", 2)
        self.assertEqual(failure.exception.code, "stale_preview")

    def test_publish_blocked_after_scheme_withdrawn(self):
        self.service.classification.withdraw_version("category", 2, "口径撤回")
        fingerprint = self.service.preview(JULY, "category", 2)["fingerprint"]
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-v2", JULY, fingerprint, "category", 2)
        self.assertEqual(failure.exception.code, "scheme_withdrawn")

    def test_frozen_release_survives_withdrawal_and_replay(self):
        preview = self.service.preview(JULY, "category", 2)
        release = self.service.publish("july-v2", JULY, preview["fingerprint"], "category", 2)
        self.service.classification.withdraw_mapping("category", 2, self.color_rule["rule_id"],
                                                     "复核确认无杂色", expected_revision=1)
        replay = self.ingest(glass_batch("batch-g1", "g1"))
        self.assertTrue(all(not row["created"] for row in replay))
        frozen = self.service.releases.get(release["release_id"])
        self.assertEqual(len(frozen["content"]["mart"]["pending_fact_ids"]), 2)
        fresh = self.service.preview(JULY, "category", 2)
        self.assertEqual(fresh["mart"]["pending_fact_ids"], [])
        later = self.service.publish("july-v2-revised", JULY, fresh["fingerprint"],
                                     "category", 2)
        diff = self.service.releases.compare(release["release_id"], later["release_id"])
        categories = {change.get("category") for change in diff["changes"]}
        self.assertIn("glass-clear", categories)
        self.assertEqual(diff["left_caliber"]["scheme_version"], 2)

    def test_post_close_releases_coexist_across_calibers(self):
        first = self.service.publish("july-final", JULY, self.service.preview(JULY)["fingerprint"])
        second = self.service.publish("july-cat-v2", JULY,
                                      self.service.preview(JULY, "category", 2)["fingerprint"],
                                      "category", 2)
        listing = self.service.snapshots.releases_for_month(JULY)
        self.assertEqual([row["name"] for row in listing["releases"]],
                         ["july-final", "july-cat-v2"])
        diff = self.service.releases.compare(first["release_id"], second["release_id"])
        self.assertEqual(diff["left_caliber"]["mode"], "as_recorded")
        self.assertEqual(diff["right_caliber"]["mode"], "scheme")
        self.assertTrue(diff["changes"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-final", JULY,
                                 self.service.preview(JULY, "category", 2)["fingerprint"],
                                 "category", 2)
        self.assertEqual(failure.exception.code, "release_conflict")

    def test_lineage_category_rows(self):
        for row in self.glass:
            self.service.classification.put_decision("category", 2, row["fact_id"],
                                                     "glass-clear", "抽检记录#7")
        current = self.service.lineage.category_row(
            JULY, "street-1", "glass-clear", {"scheme": "category", "version": 2})
        self.assertEqual(current["row"]["deposit_kg"], "10")
        self.assertEqual(current["mapping_refs"][0]["kind"], "decision")
        release = self.service.publish(
            "july-v2", JULY, self.service.preview(JULY, "category", 2)["fingerprint"],
            "category", 2)
        frozen = self.service.lineage.release_category_row(release["release_id"],
                                                           "street-1", "glass-clear")
        self.assertEqual(frozen["row"]["deposit_kg"], "10")
        self.assertEqual(frozen["caliber"]["scheme_version"], 2)
        with self.assertRaises(LakeError) as failure:
            self.service.lineage.category_row(JULY, "street-1", "glass-color",
                                              {"scheme": "category", "version": 2})
        self.assertEqual(failure.exception.code, "unknown_mart_row")


class ClassificationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)
        app = create_app(Path(self.temp.name) / "api.sqlite3")
        self.client = TestClient(app)
        self.api = app.state.service
        seed_dimensions(self.api)

    def test_full_flow_over_http(self):
        created = self.client.post("/classifications", json={
            "scheme": "category", "note": "首版",
            "categories": [{"category": "foam", "name": "泡沫"}]})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["version"], 1)
        mapping = self.client.put("/classifications/category/1/mappings", json={
            "material_id": "foam", "category": "foam", "valid_from": "2026-01-01",
            "basis": "泡沫沿用"})
        self.assertEqual(mapping.status_code, 200)
        self.assertEqual(mapping.json()["revision"], 1)
        bad = self.client.put("/classifications/category/1/mappings", json={
            "material_id": "ghost", "category": "foam", "valid_from": "2026-01-01",
            "basis": "x"})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(bad.json()["code"], "unknown_material")
        for row in balanced_batch("batch-1", "a", material_id="foam", payout="0.8"):
            self.assertEqual(self.client.post("/events", json=row).json()["state"], "normalized")
        mart = self.client.get("/months/2026-07/mart",
                               params={"scheme": "category", "scheme_version": 1})
        self.assertEqual(mart.json()["rows"][0]["category"], "foam")
        preview = self.client.get("/months/2026-07/preview",
                                  params={"scheme": "category", "scheme_version": 1}).json()
        release = self.client.post("/releases", json={
            "name": "july-cat", "month": "2026-07",
            "expected_fingerprint": preview["fingerprint"],
            "scheme": "category", "scheme_version": 1})
        self.assertEqual(release.status_code, 200)
        self.assertTrue(release.json()["created"])
        fact_id = mart.json()["rows"][0]["fact_ids"][0]
        explained = self.client.get(f"/facts/{fact_id}/classification",
                                    params={"scheme": "category", "scheme_version": 1})
        self.assertEqual(explained.json()["resolution"]["status"], "mapped")
        decision = self.client.put("/classifications/category/1/decisions/1", json={
            "category": "foam", "basis": "人工确认"})
        self.assertEqual(decision.status_code, 200)
        withdrawn = self.client.post("/classifications/category/1/decisions/1/withdraw",
                                     json={"basis": "撤销"})
        self.assertEqual(withdrawn.json()["status"], "withdrawn")
        street = self.client.get("/streets/street-1/mart", params={
            "start_month": "2026-07", "end_month": "2026-07",
            "scheme": "category", "scheme_version": 1})
        self.assertEqual(street.json()["rows"][0]["category"], "foam")
        batch = self.client.get("/batches/batch-1",
                                params={"scheme": "category", "scheme_version": 1})
        self.assertTrue(batch.json()["classification"]["consistent"])
        lineage = self.client.get("/lineage/rows/2026-07/street-1/category/foam",
                                  params={"scheme": "category", "scheme_version": 1})
        self.assertEqual(lineage.json()["row"]["deposit_kg"], "10")
        release_lineage = self.client.get(
            f"/releases/{release.json()['release_id']}/lineage/street-1/category/foam")
        self.assertEqual(release_lineage.json()["row"]["category"], "foam")
        unknown = self.client.get("/months/2026-07/mart",
                                  params={"scheme": "category", "scheme_version": 9})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["code"], "unknown_scheme_version")


if __name__ == "__main__":
    unittest.main()
