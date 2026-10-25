#!/usr/bin/env python3
"""Publish a conservative, provider-specific USD/token price snapshot (stdlib only)."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import Request, urlopen

SOURCES = {
    "litellm": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
    "models.dev": "https://models.dev/api.json",
}
RATE_FIELDS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "reasoning": "output_cost_per_reasoning_token",
    "cache_read": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
    "input_audio": "input_cost_per_audio_token",
    "output_audio": "output_cost_per_audio_token",
}
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "prices.json"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def valid_rate(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def computable(record):
    return all(valid_rate(record.get(field)) for field in
               ("input_cost_per_token", "output_cost_per_token"))


def decode(raw):
    def reject_constant(value):
        raise ValueError(f"Non-JSON numeric constant: {value}")
    return json.loads(raw, parse_constant=reject_constant)


def fetch(name, url):
    last_error = None
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "model-prices-sync/1.0 (+https://github.com/jiayuqi7813/model-prices)",
                "Accept": "application/json",
            })
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            data = decode(raw)
            validate_source(name, data)
            return data, {"url": url, "fetched_at": timestamp(),
                          "sha256": hashlib.sha256(raw).hexdigest(),
                          "count": len(data) if name == "litellm" else sum(
                              len(provider["models"]) for provider in data.values()),
                          "status": "ok"}
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Required source {name} failed: {last_error}") from last_error


def validate_source(name, data):
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{name}: expected a nonempty object")
    if name == "litellm":
        if not all(isinstance(key, str) and isinstance(value, dict)
                   for key, value in data.items()):
            raise ValueError("litellm: expected model objects")
        if not any(computable(value) for key, value in data.items() if key != "sample_spec"):
            raise ValueError("litellm: no usable token prices")
    else:
        for provider_id, provider in data.items():
            if (not isinstance(provider_id, str) or not isinstance(provider, dict)
                    or not isinstance(provider.get("models"), dict)):
                raise ValueError("models.dev: expected provider objects with model maps")
            for model_id, model in provider["models"].items():
                if (not isinstance(model_id, str) or not isinstance(model, dict)
                        or not isinstance(model.get("cost", {}), dict)):
                    raise ValueError("models.dev: malformed model or cost object")
        if not any(p["models"] for p in data.values()):
            raise ValueError("models.dev: empty model catalog")


def build(litellm, modelsdev, source_meta):
    # LiteLLM keys and every upstream dimension remain intact. sample_spec is a
    # documentation example containing fake zero prices, never a real model.
    result = {}
    for key, original in litellm.items():
        if key in ("sample_spec", "_meta"):
            continue
        record = copy.deepcopy(original)
        record["_price_provenance"] = {"source": "litellm", "source_key": key}
        record["_price_status"] = "computable" if computable(record) else "unknown"
        result[key] = record
    added = 0
    skipped = 0
    for provider_id, provider in sorted(modelsdev.items()):
        for model_id, model in sorted(provider["models"].items()):
            # Always prepend the provider, even if the model ID contains '/'.
            # Example: openrouter/openai/gpt-5 is not openai/gpt-5.
            key = f"{provider_id}/{model_id}"
            if key in result:
                skipped += 1
                continue
            costs = model.get("cost", {})
            record = {"litellm_provider": provider_id,
                      "_price_provenance": {"source": "models.dev", "provider_id": provider_id,
                                            "source_key": model_id,
                                            "source_unit": "USD per million tokens"},
                      "_source_cost": copy.deepcopy(costs)}
            for source_field, target_field in RATE_FIELDS.items():
                # Missing/invalid prices stay absent, including partial records.
                if valid_rate(costs.get(source_field)):
                    record[target_field] = costs[source_field] / 1_000_000
            limits = model.get("limit", {})
            if isinstance(limits, dict):
                for field, target in (("context", "max_tokens"), ("input", "max_input_tokens"),
                                      ("output", "max_output_tokens")):
                    if type(limits.get(field)) is int and limits[field] >= 0:
                        record[target] = limits[field]
            record["_price_status"] = "computable" if computable(record) else "unknown"
            result[key] = record
            added += 1
    result["_meta"] = {"schema_version": 1, "generated_at": timestamp(),
                       "currency": "USD", "token_rate_unit": "USD per token",
                       "sources": source_meta,
                       "record_count": len(result), "modelsdev_added": added,
                       "modelsdev_existing_keys_skipped": skipped}
    validate_feed(result)
    return result


def validate_feed(data):
    if not isinstance(data, dict) or data.get("_meta", {}).get("schema_version") != 1:
        raise ValueError("Expected schema_version 1 feed")
    meta = data["_meta"]
    if type(meta["schema_version"]) is not int:
        raise ValueError("schema_version must be an integer")
    try:
        generated = datetime.fromisoformat(meta["generated_at"].replace("Z", "+00:00"))
        if generated.utcoffset() != timezone.utc.utcoffset(generated):
            raise ValueError("generated_at must be UTC")
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError("Invalid generated_at UTC timestamp") from error
    if set(meta.get("sources", {})) != set(SOURCES):
        raise ValueError("Both required source metadata entries must be present")
    for name, details in meta["sources"].items():
        if (details.get("status") != "ok" or details.get("url") != SOURCES[name]
                or not isinstance(details.get("count"), int) or details["count"] <= 0
                or len(details.get("sha256", "")) != 64):
            raise ValueError(f"Invalid metadata for {name}")
    records = [(key, value) for key, value in data.items() if key != "_meta"]
    if not records or meta.get("record_count") != len(records):
        raise ValueError("Empty feed or record count mismatch")
    usable = 0
    for key, record in records:
        if not isinstance(record, dict) or not record.get("_price_provenance"):
            raise ValueError(f"Missing record/provenance: {key}")
        expected = "computable" if computable(record) else "unknown"
        if record.get("_price_status") != expected:
            raise ValueError(f"Incorrect price status: {key}")
        usable += expected == "computable"
    if not usable:
        raise ValueError("Feed has no computable records")
    return usable


def atomic_write(path, data):
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="validate existing output without network or writes")
    args = parser.parse_args()
    try:
        if args.check:
            data = decode(args.output.read_bytes())
        else:
            data_by_source, meta = {}, {}
            # Nothing is written until both required sources fetch and validate.
            for name, url in SOURCES.items():
                data_by_source[name], meta[name] = fetch(name, url)
                print(f"Fetched {name}: {meta[name]['count']} source entries", flush=True)
            data = build(data_by_source["litellm"], data_by_source["models.dev"], meta)
            atomic_write(args.output, data)
        usable = validate_feed(data)
        print(f"Validated {len(data) - 1} records ({usable} computable): {args.output}")
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
