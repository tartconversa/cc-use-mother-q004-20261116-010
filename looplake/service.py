from __future__ import annotations

from .batch import BatchImport
from .classification import Classification
from .dimensions import Dimensions
from .landing import Landing
from .ledger import Ledger
from .lineage import Lineage
from .mart import Mart
from .model import LakeError, key
from .quality import Quality
from .reconcile import Reconciliation
from .release import Release
from .store import Store
from .settlement import Settlement
from .inventory import Inventory
from .query import Query
from .snapshot import SnapshotAudit


class LakeService:
    def __init__(self, database_path):
        self.store = Store(database_path)
        self.dimensions = Dimensions(self.store)
        self.landing = Landing(self.store, self.dimensions)
        self.ledger = Ledger(self.store, self.dimensions, self.landing)
        self.inventory = Inventory(self.ledger)
        self.query = Query(self.ledger)
        self.settlement = Settlement(self.store)
        self.classification = Classification(self.store)
        self.reconciliation = Reconciliation(self.ledger, self.settlement, self.classification)
        self.mart = Mart(self.ledger, self.reconciliation, self.classification)
        self.releases = Release(self.store, self.mart, self.reconciliation, self.inventory)
        self.snapshots = SnapshotAudit(self.store)
        self.quality = Quality(self.store, self.landing, self.ledger,
                               self.reconciliation, self.mart)
        self.lineage = Lineage(self.store, self.ledger, self.reconciliation,
                               self.mart, self.releases)
        self.batches = BatchImport(self.ledger)

    @staticmethod
    def caliber(scheme, scheme_version):
        if scheme is None and scheme_version is None:
            return None
        if scheme is None or scheme_version is None:
            raise LakeError("invalid_caliber", "scheme and scheme_version are required together")
        if isinstance(scheme_version, bool) or not isinstance(scheme_version, int):
            raise LakeError("invalid_caliber", "scheme_version must be an integer")
        return {"scheme": key(scheme, "scheme"), "version": scheme_version}

    def register_source(self, source, channel, expected_version=None):
        return self.dimensions.register_source(source, channel, expected_version)

    def register_dimension(self, data, expected_version=None):
        return self.dimensions.put(data, expected_version)

    def ingest(self, data):
        return self.ledger.ingest(data)

    def retry(self, ingestion_id):
        return self.ledger.normalize(ingestion_id)

    def batch_status(self, batch_id, scheme=None, scheme_version=None):
        return self.reconciliation.batch(batch_id, caliber=self.caliber(scheme, scheme_version))

    def month_status(self, month):
        return self.quality.month(month)

    def month_report(self, month, scheme=None, scheme_version=None):
        return self.mart.month(month, caliber=self.caliber(scheme, scheme_version))

    def street_report(self, street_id, start_month, end_month, scheme=None, scheme_version=None):
        return self.mart.street(street_id, start_month, end_month,
                                caliber=self.caliber(scheme, scheme_version))

    def preview(self, month, scheme=None, scheme_version=None):
        return self.releases.preview(month, self.caliber(scheme, scheme_version))

    def publish(self, name, month, fingerprint, scheme=None, scheme_version=None):
        return self.releases.publish(name, month, fingerprint,
                                     self.caliber(scheme, scheme_version))

    def fact_classification(self, fact_id, scheme, scheme_version):
        caliber = self.caliber(scheme, scheme_version)
        if caliber is None:
            raise LakeError("invalid_caliber", "scheme and scheme_version are required")
        return self.classification.explain(fact_id, caliber["scheme"], caliber["version"])
