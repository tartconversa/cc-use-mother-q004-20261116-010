from __future__ import annotations

from .model import LakeError, calendar_day, key, required
from .store import Store

ACTIVE = "active"
WITHDRAWN = "withdrawn"


def _basis(value) -> str:
    return required(value, "basis", 500)


class Classification:
    """可审核的分类版本、映射与逐事实裁定。

    映射与裁定均为只追加的修订链：修订产生新 revision，撤回是 status='withdrawn'
    的修订，历史 revision 永不改写，因此任何时刻的归类依据都可追溯。
    """

    def __init__(self, store: Store):
        self.store = store

    # ---------- 分类版本 ----------

    def create_version(self, scheme: str, categories: list, note: str = "",
                       expected_version: int | None = None):
        scheme = key(scheme, "scheme")
        if not isinstance(categories, list) or not 1 <= len(categories) <= 50:
            raise LakeError("invalid_categories", "categories must list 1..50 entries")
        parsed, seen = [], set()
        for entry in categories:
            if not isinstance(entry, dict):
                raise LakeError("invalid_categories", "category must be an object")
            token = key(entry.get("category"), "category")
            name = required(entry.get("name"), "name", 200)
            if token in seen:
                raise LakeError("duplicate_category", f"category {token} repeated")
            seen.add(token)
            parsed.append((token, name))
        note = note if isinstance(note, str) and len(note) <= 500 else None
        if note is None:
            raise LakeError("invalid_note", "note must be text up to 500 characters")
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT MAX(version) AS v FROM classification_schemes WHERE scheme=?",
                                 (scheme,))
            actual = old["v"] or 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_scheme", "scheme version changed", 409)
            version = actual + 1
            cursor = conn.execute("INSERT INTO classification_schemes(scheme,version,status,note,created_at) VALUES(?,?,?,?,datetime('now'))",
                                  (scheme, version, ACTIVE, note))
            for token, name in parsed:
                conn.execute("INSERT INTO classification_categories(scheme,version,category,name) VALUES(?,?,?,?)",
                             (scheme, version, token, name))
            self.store.audit(conn, "classification_version_added", f"{scheme}:{version}",
                             {"categories": [token for token, _ in parsed], "note": note})
        return {"scheme": scheme, "version": version, "status": ACTIVE,
                "categories": [{"category": token, "name": name} for token, name in parsed]}

    def withdraw_version(self, scheme: str, version: int, basis: str):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        basis = _basis(basis)
        with self.store.write() as conn:
            row = self._version_row(conn, scheme, version)
            if row["status"] != ACTIVE:
                raise LakeError("scheme_withdrawn", "scheme version already withdrawn", 409)
            conn.execute("UPDATE classification_schemes SET status=? WHERE scheme=? AND version=?",
                         (WITHDRAWN, scheme, version))
            self.store.audit(conn, "classification_version_withdrawn", f"{scheme}:{version}",
                             {"basis": basis})
        return {"scheme": scheme, "version": version, "status": WITHDRAWN}

    def versions(self, scheme: str):
        scheme = key(scheme, "scheme")
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM classification_schemes WHERE scheme=? ORDER BY version",
                                  (scheme,))
            for row in rows:
                row["categories"] = self.store.all(
                    conn, "SELECT category,name FROM classification_categories WHERE scheme=? AND version=? ORDER BY category",
                    (scheme, row["version"]))
        return rows

    @staticmethod
    def _version_number(version) -> int:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise LakeError("invalid_scheme_version", "scheme version must be a positive integer")
        return version

    def _version_row(self, conn, scheme: str, version: int):
        row = self.store.one(conn, "SELECT * FROM classification_schemes WHERE scheme=? AND version=?",
                             (scheme, version))
        if row is None:
            raise LakeError("unknown_scheme_version", f"{scheme}:{version} is not registered", 404)
        return row

    def _active_version_row(self, conn, scheme: str, version: int):
        row = self._version_row(conn, scheme, version)
        if row["status"] != ACTIVE:
            raise LakeError("scheme_withdrawn", f"{scheme}:{version} is withdrawn", 409)
        return row

    def _require_category(self, conn, scheme: str, version: int, category: str):
        row = self.store.one(conn, "SELECT id FROM classification_categories WHERE scheme=? AND version=? AND category=?",
                             (scheme, version, category))
        if row is None:
            raise LakeError("unknown_category", f"{category} is not in {scheme}:{version}", 409)

    # ---------- 映射 ----------

    def put_mapping(self, scheme: str, version: int, material_id: str, category: str,
                    valid_from: str, basis: str, valid_to: str | None = None,
                    expected_revision: int | None = None):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        material_id = key(material_id, "material_id")
        category = key(category, "category")
        valid_from = calendar_day(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = calendar_day(valid_to, "valid_to")
            if valid_to <= valid_from:
                raise LakeError("invalid_range", "valid_to must follow valid_from")
        basis = _basis(basis)
        with self.store.write() as conn:
            self._active_version_row(conn, scheme, version)
            self._require_category(conn, scheme, version, category)
            material = self.store.one(conn, "SELECT id FROM dimension_versions WHERE kind='material' AND external_id=?",
                                      (material_id,))
            if material is None:
                raise LakeError("unknown_material", "mapping material must be registered", 409)
            latest = self._latest_mapping(conn, scheme, version, material_id, category, valid_from)
            actual = latest["revision"] if latest else 0
            if expected_revision is not None and expected_revision != actual:
                raise LakeError("stale_mapping", "mapping revision changed", 409)
            if latest and latest["status"] == ACTIVE and latest["valid_to"] == valid_to \
                    and latest["basis"] == basis:
                return {"rule_id": latest["rule_id"], "revision": latest["revision"],
                        "status": ACTIVE, "created": False}
            self._check_category_overlap(conn, scheme, version, material_id, category,
                                         valid_from, valid_to,
                                         exclude_rule=latest["rule_id"] if latest else None)
            revision = actual + 1
            cursor = conn.execute(
                "INSERT INTO classification_mappings(rule_id,scheme,version,material_id,category,valid_from,valid_to,basis,revision,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (latest["rule_id"] if latest else None, scheme, version, material_id,
                 category, valid_from, valid_to, basis, revision, ACTIVE))
            rule_id = latest["rule_id"] if latest else cursor.lastrowid
            if latest is None:
                conn.execute("UPDATE classification_mappings SET rule_id=? WHERE id=?",
                             (rule_id, cursor.lastrowid))
            self.store.audit(conn, "mapping_revised" if latest else "mapping_added",
                             f"{scheme}:{version}:{material_id}",
                             {"rule_id": rule_id, "revision": revision, "category": category,
                              "valid_from": valid_from, "valid_to": valid_to, "basis": basis})
        return {"rule_id": rule_id, "revision": revision, "status": ACTIVE, "created": True}

    def withdraw_mapping(self, scheme: str, version: int, rule_id: int, basis: str,
                         expected_revision: int | None = None):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        if isinstance(rule_id, bool) or not isinstance(rule_id, int) or rule_id < 1:
            raise LakeError("invalid_rule", "rule_id must be a positive integer")
        basis = _basis(basis)
        with self.store.write() as conn:
            self._active_version_row(conn, scheme, version)
            latest = self.store.one(conn,
                                    "SELECT * FROM classification_mappings WHERE rule_id=? AND scheme=? AND version=?"
                                    " ORDER BY revision DESC LIMIT 1",
                                    (rule_id, scheme, version))
            if latest is None:
                raise LakeError("unknown_mapping", "mapping rule not found", 404)
            if latest["status"] != ACTIVE:
                raise LakeError("mapping_withdrawn", "mapping rule already withdrawn", 409)
            if expected_revision is not None and expected_revision != latest["revision"]:
                raise LakeError("stale_mapping", "mapping revision changed", 409)
            revision = latest["revision"] + 1
            conn.execute(
                "INSERT INTO classification_mappings(rule_id,scheme,version,material_id,category,valid_from,valid_to,basis,revision,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (rule_id, scheme, version, latest["material_id"], latest["category"],
                 latest["valid_from"], latest["valid_to"], basis, revision, WITHDRAWN))
            self.store.audit(conn, "mapping_withdrawn", f"{scheme}:{version}:{latest['material_id']}",
                             {"rule_id": rule_id, "revision": revision, "basis": basis})
        return {"rule_id": rule_id, "revision": revision, "status": WITHDRAWN}

    def _latest_mapping(self, conn, scheme, version, material_id, category, valid_from):
        return self.store.one(conn,
                              "SELECT * FROM classification_mappings WHERE scheme=? AND version=? AND material_id=?"
                              " AND category=? AND valid_from=? ORDER BY revision DESC LIMIT 1",
                              (scheme, version, material_id, category, valid_from))

    def _check_category_overlap(self, conn, scheme, version, material_id, category,
                                valid_from, valid_to, exclude_rule):
        rows = [row for row in self._latest_mappings(conn, scheme, version)
                if row["status"] == ACTIVE and row["material_id"] == material_id
                and row["category"] == category and row["rule_id"] != exclude_rule]
        for row in rows:
            if (row["valid_to"] is None or row["valid_to"] > valid_from) and \
                    (valid_to is None or row["valid_from"] < valid_to):
                raise LakeError("mapping_overlap",
                                "same-category mapping periods overlap", 409)

    @staticmethod
    def _latest_mappings(conn, scheme, version):
        rows = Store.all(conn, "SELECT * FROM classification_mappings WHERE scheme=? AND version=?",
                         (scheme, version))
        latest = {}
        for row in rows:
            if row["rule_id"] not in latest or row["revision"] > latest[row["rule_id"]]["revision"]:
                latest[row["rule_id"]] = row
        return list(latest.values())

    def mappings(self, scheme: str, version: int):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        with self.store.connect() as conn:
            self._version_row(conn, scheme, version)
            return self.store.all(conn,
                                  "SELECT * FROM classification_mappings WHERE scheme=? AND version=?"
                                  " ORDER BY rule_id, revision",
                                  (scheme, version))

    # ---------- 逐事实裁定 ----------

    def put_decision(self, scheme: str, version: int, fact_id: int, category: str, basis: str,
                     expected_revision: int | None = None):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        category = key(category, "category")
        basis = _basis(basis)
        fact = self.store.fact(fact_id)
        with self.store.write() as conn:
            self._active_version_row(conn, scheme, version)
            self._require_category(conn, scheme, version, category)
            latest = self._latest_decision(conn, scheme, version, fact_id)
            actual = latest["revision"] if latest else 0
            if expected_revision is not None and expected_revision != actual:
                raise LakeError("stale_decision", "decision revision changed", 409)
            if latest and latest["status"] == ACTIVE and latest["category"] == category \
                    and latest["basis"] == basis:
                return {"decision_id": latest["id"], "revision": latest["revision"],
                        "status": ACTIVE, "created": False}
            revision = actual + 1
            cursor = conn.execute(
                "INSERT INTO classification_decisions(scheme,version,fact_id,category,basis,revision,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,datetime('now'))",
                (scheme, version, fact["id"], category, basis, revision, ACTIVE))
            self.store.audit(conn, "decision_revised" if latest else "decision_added",
                             f"{scheme}:{version}:fact:{fact_id}",
                             {"decision_id": cursor.lastrowid, "revision": revision,
                              "category": category, "basis": basis})
            return {"decision_id": cursor.lastrowid, "revision": revision,
                    "status": ACTIVE, "created": True}

    def withdraw_decision(self, scheme: str, version: int, fact_id: int, basis: str,
                          expected_revision: int | None = None):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        basis = _basis(basis)
        self.store.fact(fact_id)
        with self.store.write() as conn:
            self._active_version_row(conn, scheme, version)
            latest = self._latest_decision(conn, scheme, version, fact_id)
            if latest is None or latest["status"] != ACTIVE:
                raise LakeError("unknown_decision", "no active decision for fact", 404)
            if expected_revision is not None and expected_revision != latest["revision"]:
                raise LakeError("stale_decision", "decision revision changed", 409)
            revision = latest["revision"] + 1
            cursor = conn.execute(
                "INSERT INTO classification_decisions(scheme,version,fact_id,category,basis,revision,status,created_at)"
                " VALUES(?,?,?,NULL,?,?,?,datetime('now'))",
                (scheme, version, fact_id, basis, revision, WITHDRAWN))
            self.store.audit(conn, "decision_withdrawn", f"{scheme}:{version}:fact:{fact_id}",
                             {"decision_id": cursor.lastrowid, "revision": revision, "basis": basis})
            return {"decision_id": cursor.lastrowid, "revision": revision, "status": WITHDRAWN}

    def _latest_decision(self, conn, scheme, version, fact_id):
        return self.store.one(conn,
                              "SELECT * FROM classification_decisions WHERE scheme=? AND version=? AND fact_id=?"
                              " ORDER BY revision DESC LIMIT 1",
                              (scheme, version, fact_id))

    def decisions(self, scheme: str, version: int):
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        with self.store.connect() as conn:
            self._version_row(conn, scheme, version)
            return self.store.all(conn,
                                  "SELECT * FROM classification_decisions WHERE scheme=? AND version=?"
                                  " ORDER BY fact_id, revision",
                                  (scheme, version))

    # ---------- 解析 ----------

    def snapshot(self, scheme: str, version: int):
        """某一分类版本当前有效的映射与裁定快照，供报表一次性解析。"""
        scheme = key(scheme, "scheme")
        version = self._version_number(version)
        with self.store.connect() as conn:
            row = self._version_row(conn, scheme, version)
            categories = self.store.all(conn,
                                        "SELECT category,name FROM classification_categories WHERE scheme=? AND version=? ORDER BY category",
                                        (scheme, version))
            rules = [self._rule_public(item)
                     for item in self._latest_mappings(conn, scheme, version)
                     if item["status"] == ACTIVE]
            decisions = {}
            for item in self.store.all(conn,
                                       "SELECT * FROM classification_decisions WHERE scheme=? AND version=? ORDER BY revision",
                                       (scheme, version)):
                if item["status"] == ACTIVE:
                    decisions[item["fact_id"]] = {"kind": "decision", "decision_id": item["id"],
                                                  "revision": item["revision"], "fact_id": item["fact_id"],
                                                  "category": item["category"], "basis": item["basis"]}
                else:
                    decisions.pop(item["fact_id"], None)
        rules.sort(key=lambda item: (item["material_id"], item["rule_id"]))
        return {"scheme": scheme, "version": version, "status": row["status"],
                "note": row["note"], "categories": categories,
                "rules": rules, "decisions": decisions}

    @staticmethod
    def _rule_public(row):
        return {"kind": "mapping", "rule_id": row["rule_id"], "revision": row["revision"],
                "material_id": row["material_id"], "category": row["category"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "basis": row["basis"]}

    @staticmethod
    def resolve(snapshot: dict, material_id: str | None, day: str, fact_id: int | None = None):
        """单条事实在快照下的归类；无法唯一确定时保持待判，绝不猜测。"""
        if fact_id is not None and fact_id in snapshot["decisions"]:
            decision = snapshot["decisions"][fact_id]
            return {"status": "mapped", "category": decision["category"], "ref": decision}
        if material_id is None:
            return {"status": "pending", "reason": "no_material", "candidates": []}
        rules = [rule for rule in snapshot["rules"]
                 if rule["material_id"] == material_id and rule["valid_from"] <= day
                 and (rule["valid_to"] is None or day < rule["valid_to"])]
        if not rules:
            return {"status": "pending", "reason": "unmapped", "candidates": []}
        categories = {rule["category"] for rule in rules}
        if len(categories) == 1:
            return {"status": "mapped", "category": rules[0]["category"], "ref": rules[0]}
        return {"status": "pending", "reason": "ambiguous", "candidates": rules}

    def explain(self, fact_id: int, scheme: str, version: int):
        """追溯单条事实在某分类版本下的归类依据（含裁定历史与候选映射）。"""
        fact = self.store.fact(fact_id)
        snap = self.snapshot(scheme, version)
        material = fact["material_id"]
        inherited = None
        if material is None and fact["event_type"] == "payout":
            locations = {(row["street_id"], row["material_id"])
                         for row in self.store.facts("WHERE batch_id=?", (fact["batch_id"],))
                         if row["event_type"] == "deposit"}
            if len(locations) == 1:
                street, material = next(iter(locations))
                inherited = {"street_id": street, "material_id": material}
        resolution = self.resolve(snap, material, fact["day"], fact_id)
        history = [row for row in self.decisions(scheme, version) if row["fact_id"] == fact_id]
        return {"fact_id": fact_id, "event_type": fact["event_type"], "day": fact["day"],
                "material_id": fact["material_id"], "inherited": inherited,
                "resolved_material": material,
                "caliber": {"mode": "scheme", "scheme": snap["scheme"],
                            "scheme_version": snap["version"], "scheme_status": snap["status"]},
                "resolution": resolution, "decision_history": history}
