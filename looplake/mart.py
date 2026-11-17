from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .classification import Classification
from .ledger import Ledger
from .model import LakeError
from .reconcile import Reconciliation


class Mart:
    def __init__(self, ledger: Ledger, reconciliation: Reconciliation,
                 classification: Classification | None = None):
        self.ledger, self.reconciliation = ledger, reconciliation
        self.classification = classification

    @staticmethod
    def _number(value: Decimal):
        return format(value, "f")

    def _snapshot(self, caliber: dict | None):
        if caliber is None:
            return None
        if self.classification is None:
            raise LakeError("classification_unavailable", "classification is not configured", 409)
        return self.classification.snapshot(caliber["scheme"], caliber["version"])

    @staticmethod
    def _caliber_info(snap):
        if snap is None:
            return {"mode": "as_recorded"}
        return {"mode": "scheme", "scheme": snap["scheme"], "scheme_version": snap["version"],
                "scheme_status": snap["status"]}

    def month(self, month: str, include_unresolved: bool = False, caliber: dict | None = None):
        facts = self.ledger.period(month)
        statuses = {batch: self.reconciliation.batch(batch) for batch in sorted({f["batch_id"] for f in facts})}
        selected_batches = {batch for batch, status in statuses.items() if status["ready"] or include_unresolved}
        snap = self._snapshot(caliber)
        buckets = defaultdict(lambda: {"weight_kg": Decimal("0"), "payout_yuan": Decimal("0"),
                                       "deposit_events": 0, "payout_events": 0, "fact_ids": [],
                                       "refs": {}})
        batch_locations = {}
        for fact in facts:
            if fact["event_type"] == "deposit":
                batch_locations.setdefault(fact["batch_id"], set()).add((fact["street_id"], fact["material_id"]))
        excluded = []
        pending = []
        for fact in facts:
            if fact["batch_id"] not in selected_batches:
                excluded.append(fact["id"])
                continue
            if fact["event_type"] not in {"deposit", "payout"}:
                continue
            if fact["event_type"] == "payout":
                destinations = batch_locations.get(fact["batch_id"], set())
                if len(destinations) != 1:
                    excluded.append(fact["id"])
                    continue
                location, material = next(iter(destinations))
            else:
                location, material = fact["street_id"], fact["material_id"]
            location = location or "unassigned"
            ref = None
            if snap is None:
                group = material or "unassigned"
            else:
                resolution = Classification.resolve(snap, material, fact["day"], fact["id"])
                if resolution["status"] != "mapped":
                    pending.append({"fact_id": fact["id"], "batch_id": fact["batch_id"],
                                    "event_type": fact["event_type"], "day": fact["day"],
                                    "street_id": location, "material_id": material,
                                    "weight_kg": fact["weight_kg"],
                                    "payout_yuan": fact["payout_yuan"],
                                    "reason": resolution["reason"],
                                    "candidates": resolution.get("candidates", [])})
                    continue
                group = resolution["category"]
                ref = resolution["ref"]
            bucket = buckets[(location, group)]
            if ref is not None:
                marker = (ref["kind"], ref.get("rule_id") or ref.get("decision_id") or 0,
                          ref["revision"], ref["category"], ref.get("fact_id") or 0)
                bucket["refs"][marker] = ref
            if fact["event_type"] == "deposit":
                bucket["weight_kg"] += Decimal(fact["weight_kg"] or "0")
                bucket["deposit_events"] += 1
            else:
                bucket["payout_yuan"] += Decimal(fact["payout_yuan"] or "0")
                bucket["payout_events"] += 1
            bucket["fact_ids"].append(fact["id"])
        rows = []
        for key, value in sorted(buckets.items()):
            row = {"month": month, "street_id": key[0],
                   "deposit_kg": self._number(value["weight_kg"]),
                   "payout_yuan": self._number(value["payout_yuan"]),
                   "deposit_events": value["deposit_events"], "payout_events": value["payout_events"],
                   "fact_ids": sorted(value["fact_ids"])}
            if snap is None:
                row["material_id"] = key[1]
            else:
                row["category"] = key[1]
                row["mapping_refs"] = [value["refs"][marker] for marker in sorted(value["refs"])]
            rows.append(row)
        result = {"month": month, "caliber": self._caliber_info(snap),
                  "ready": bool(statuses) and all(r["ready"] for r in statuses.values()),
                  "rows": rows, "excluded_fact_ids": excluded,
                  "unresolved_batches": [batch for batch, status in statuses.items() if not status["ready"]],
                  "batch_count": len(statuses)}
        if snap is not None:
            result["pending"] = pending
            result["pending_fact_ids"] = sorted(item["fact_id"] for item in pending)
        return result

    def street(self, street_id: str, start_month: str, end_month: str, caliber: dict | None = None):
        from .model import key
        street_id = key(street_id, "street_id")
        if start_month > end_month:
            raise LakeError("invalid_range", "start_month follows end_month")
        facts = [row for row in self.ledger.store.facts("WHERE street_id=?", (street_id,))
                 if start_month <= row["day"][:7] <= end_month]
        months = sorted({row["day"][:7] for row in facts})
        result = []
        pending = []
        for month in months:
            report = self.month(month, caliber=caliber)
            result.extend(row for row in report["rows"] if row["street_id"] == street_id)
            pending.extend(item for item in report.get("pending", []) if item["street_id"] == street_id)
        response = {"street_id": street_id, "start_month": start_month, "end_month": end_month,
                    "caliber": self._caliber_info(self._snapshot(caliber)),
                    "rows": result}
        if caliber is not None:
            response["pending"] = pending
        return response

    def material(self, material_id: str, month: str):
        from .model import key
        material_id = key(material_id, "material_id")
        report = self.month(month)
        rows = [row for row in report["rows"] if row["material_id"] == material_id]
        total = sum((Decimal(row["deposit_kg"]) for row in rows), Decimal("0"))
        return {"material_id": material_id, "month": month, "deposit_kg": self._number(total),
                "rows": rows, "ready": report["ready"]}
