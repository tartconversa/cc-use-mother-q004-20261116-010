from __future__ import annotations

import argparse
import json
import sys

from .model import LakeError
from .service import LakeService


def parser():
    p = argparse.ArgumentParser(prog="looplake")
    p.add_argument("--db", default="looplake.sqlite3")
    sub = p.add_subparsers(dest="command", required=True)
    source = sub.add_parser("source")
    source.add_argument("source")
    source.add_argument("channel")
    dimension = sub.add_parser("dimension")
    dimension.add_argument("file")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("file")
    batch = sub.add_parser("batch")
    batch.add_argument("file")
    batch.add_argument("checkpoint")
    batch.add_argument("--limit", type=int)
    rate = sub.add_parser("rate")
    rate.add_argument("material_id")
    rate.add_argument("valid_from")
    rate.add_argument("rate_yuan_per_kg")
    check = sub.add_parser("check")
    check.add_argument("batch_id")
    mart = sub.add_parser("mart")
    mart.add_argument("month")
    mart.add_argument("--scheme")
    mart.add_argument("--scheme-version", type=int)
    street = sub.add_parser("street-mart")
    street.add_argument("street_id")
    street.add_argument("start_month")
    street.add_argument("end_month")
    street.add_argument("--scheme")
    street.add_argument("--scheme-version", type=int)
    preview = sub.add_parser("preview")
    preview.add_argument("month")
    preview.add_argument("--scheme")
    preview.add_argument("--scheme-version", type=int)
    release = sub.add_parser("release")
    release.add_argument("name")
    release.add_argument("month")
    release.add_argument("fingerprint")
    release.add_argument("--scheme")
    release.add_argument("--scheme-version", type=int)
    scheme = sub.add_parser("scheme")
    scheme.add_argument("file")
    mapping = sub.add_parser("mapping")
    mapping.add_argument("scheme")
    mapping.add_argument("version", type=int)
    mapping.add_argument("file")
    decide = sub.add_parser("decide")
    decide.add_argument("scheme")
    decide.add_argument("version", type=int)
    decide.add_argument("fact_id", type=int)
    decide.add_argument("category")
    decide.add_argument("basis")
    explain = sub.add_parser("explain")
    explain.add_argument("fact_id", type=int)
    explain.add_argument("scheme")
    explain.add_argument("version", type=int)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("release_id", type=int)
    return p


def execute(service: LakeService, args):
    if args.command == "source":
        return service.register_source(args.source, args.channel)
    if args.command == "dimension":
        with open(args.file, encoding="utf-8") as file:
            return service.register_dimension(json.load(file))
    if args.command == "ingest":
        with open(args.file, encoding="utf-8") as file:
            return service.ingest(json.load(file))
    if args.command == "batch":
        return service.batches.resume(args.file, args.checkpoint, args.limit)
    if args.command == "rate":
        return service.settlement.put_rate(args.material_id, "smart_box", args.valid_from,
                                           args.rate_yuan_per_kg)
    if args.command == "check":
        return service.batch_status(args.batch_id)
    if args.command == "mart":
        return service.month_report(args.month, args.scheme, args.scheme_version)
    if args.command == "street-mart":
        return service.street_report(args.street_id, args.start_month, args.end_month,
                                     args.scheme, args.scheme_version)
    if args.command == "preview":
        return service.preview(args.month, args.scheme, args.scheme_version)
    if args.command == "release":
        return service.publish(args.name, args.month, args.fingerprint,
                               args.scheme, args.scheme_version)
    if args.command == "scheme":
        with open(args.file, encoding="utf-8") as file:
            data = json.load(file)
        return service.classification.create_version(data["scheme"], data["categories"],
                                                     data.get("note", ""))
    if args.command == "mapping":
        with open(args.file, encoding="utf-8") as file:
            data = json.load(file)
        return service.classification.put_mapping(args.scheme, args.version, data["material_id"],
                                                  data["category"], data["valid_from"],
                                                  data["basis"], data.get("valid_to"))
    if args.command == "decide":
        return service.classification.put_decision(args.scheme, args.version, args.fact_id,
                                                   args.category, args.basis)
    if args.command == "explain":
        return service.fact_classification(args.fact_id, args.scheme, args.version)
    if args.command == "inspect":
        return service.releases.get(args.release_id)
    raise LakeError("unknown_command", "unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(LakeService(args.db), args)
    except (LakeError, OSError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, LakeError):
            output = {"code": exc.code, "detail": exc.detail}
        else:
            output = {"code": "input_error", "detail": str(exc)}
        print(json.dumps(output, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
