#!/usr/bin/env python3
"""Publish a conservative, provider-specific USD/token price snapshot (stdlib only)."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import re
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
    "cursor": "https://cursor.com/docs/models-and-pricing",
    "devin": "https://docs.devin.ai/desktop/models.md",
    "grok": "https://docs.x.ai/developers/pricing.md",
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


# These are verified official Cursor model/catalog IDs, never IDs synthesized from display labels.
# Keep this allowlist explicit: new upstream labels require mapping review.
CURSOR_ID_CATALOG = "https://cursor.com/docs-static/_next/static/immutable/chunks/2gzgn5h9nqkyy.js"
CURSOR_MODELS = {
    "Composer 2.5": ("composer-2.5", "standard"),
    "Composer 2.5 (Fast)": ("composer-2.5-fast", "fast"),
    "Grok 4.7": ("grok-4.7", "standard"),
    "Grok 4.7 (Fast)": ("grok-4.7-fast", "fast"),
    "Grok 4.7 500k": ("grok-4.7-long", "500k"),
    "Grok 4.7 500k (Fast)": ("grok-4.7-fast-long", "500k fast"),
    "Grok 4.6": ("grok-4.6", "standard"),
    "Grok 4.6 (Fast)": ("grok-4.6-fast", "fast"),
    "Grok 4.5": ("grok-4.5", "standard"),
    "Grok 4.5 (Fast)": ("grok-4.5-fast", "fast"),
    "Claude Fable 5.1": ("claude-fable-5-1", "standard"),
    "Claude Haiku 5.5": ("claude-haiku-5-5", "standard"),
    "Claude Opus 5.5": ("claude-opus-5-5", "standard"),
    "Claude Sonnet 5.5": ("claude-sonnet-5-5", "standard"),
    "Gemini 3.1 Pro": ("gemini-3.1-pro", "standard"),
    "Gemini 3.8 Flash": ("gemini-3.8-flash", "standard"),
    "Gemini 4 Argon": ("gemini-4-argon", "standard"),
    "GPT-5.6 Luna": ("gpt-5.6-luna", "standard"),
    "GPT-5.6 Sol": ("gpt-5.6-sol", "standard"),
    "GPT-5.6 Terra": ("gpt-5.6-terra", "standard"),
    "Muse Wonder": ("muse-wonder-1.0", "standard"),
}


class PricingTables(HTMLParser):
    """Read only HTML table cells; scripts and surrounding prose are not data."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table is not None:
                raise ValueError("cursor: nested pricing table")
            self.table = []
        elif self.table is not None and tag == "tr":
            self.row = []
        elif self.row is not None and tag in ("th", "td"):
            self.cell = []
        elif self.cell is not None and tag == "br":
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def cursor_rate(cell):
    if cell.strip() in ("-", "–", "—"):
        return None
    match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", cell.strip())
    if not match:
        raise ValueError(f"cursor: unrecognized USD/million price {cell!r}")
    value = float(match.group(1))
    if not valid_rate(value):
        raise ValueError("cursor: invalid price")
    return value


def parse_cursor(raw):
    parser = PricingTables()
    parser.feed(raw.decode("utf-8"))
    parser.close()
    required = ("Name", "Input", "Cache Write", "Cache Read", "Output")
    records = {}
    unmapped = []
    rows_seen = set()
    for table in parser.tables:
        if not table or not all(header in table[0] for header in required):
            continue
        headers = table[0]
        positions = {header: headers.index(header) for header in required}
        for row in table[1:]:
            if not row:
                continue
            if len(row) != len(headers):
                raise ValueError("cursor: malformed pricing table row")
            label = row[positions["Name"]]
            costs = {field: cursor_rate(row[positions[header]]) for field, header in
                     (("input", "Input"), ("cache_write", "Cache Write"),
                      ("cache_read", "Cache Read"), ("output", "Output"))}
            if costs["input"] is None or costs["output"] is None:
                raise ValueError(f"cursor: missing base token prices for {label}")
            rows_seen.add(label)
            mapped = CURSOR_MODELS.get(label)
            if mapped is None:
                unmapped.append(label)
                continue
            model_id, variant = mapped
            record = {"name": label, "cost": {k: v for k, v in costs.items()
                                               if v is not None}, "variant": variant}
            if model_id in records and records[model_id] != record:
                raise ValueError(f"cursor: conflicting duplicate model ID {model_id}")
            records[model_id] = record
    if not records:
        raise ValueError("cursor: no mapped official token pricing rows")
    return {"models": records, "pricing_rows": len(rows_seen),
            "unmapped_names": sorted(set(unmapped))}


# Verified public model_uid values from the official HTML model catalog.
DEVIN_MODEL_CATALOG = "https://docs.devin.ai/desktop/models"
DEVIN_MODELS = {"swe-2-medium": "Medium", "swe-2-high": "High", "swe-2-max": "Max"}


def parse_devin(raw):
    # Mintlify can escape tab labels and dollar signs more than once.
    text = re.sub(r"\\+([\[\]$])", r"\1", raw.decode("utf-8"))
    # Only the explicit self-serve SWE-2 list-price sentence is authoritative
    # here. Never read account tables, promotion prices or enterprise discounts.
    section = re.search(
        r"(?:\*\*Self-serve\*\*|\[tab\][ \t]+Self-serve)(.*?)"
        r"(?:\*\*Enterprise \(ACUs\)\*\*|\[tab\][ \t]+Enterprise \(ACUs\))",
        text, re.S)
    if section is None:
        raise ValueError("devin: public self-serve section not found")
    amount = r"\$([0-9]+(?:\.[0-9]+)?)/1M"
    pattern = (r"SWE-2[^\n]*?List pricing \(" + amount + r" input, " + amount
               + r" output, " + amount + r" cache input\) applies afterward\.")
    matches = re.findall(pattern, section.group(1))
    if len(matches) != 1:
        raise ValueError("devin: unique SWE-2 public list-price statement not found")
    if not re.search(r"SWE-2[^\n]*Medium, High, and Max", text):
        raise ValueError("devin: SWE-2 reasoning variants not documented")
    costs = dict(zip(("input", "output", "cache_read"), map(float, matches[0])))
    if not all(valid_rate(value) for value in costs.values()):
        raise ValueError("devin: invalid public list price")
    return {"models": {model_id: {"name": f"SWE-2 {effort}", "cost": dict(costs),
                                   "variant": effort.lower()}
                       for model_id, effort in DEVIN_MODELS.items()}}


def add_devin_records(result, devin):
    replaced = 0
    for model_id, model in sorted(devin["models"].items()):
        key = f"devin/{model_id}"
        replaced += key in result
        record = {"litellm_provider": "devin", "mode": "chat", "name": model["name"],
                  "_price_provenance": {"source": "devin", "source_key": model_id,
                                        "source_name": model["name"], "url": SOURCES["devin"],
                                        "model_id_catalog_url": DEVIN_MODEL_CATALOG,
                                        "source_unit": "USD per million tokens"},
                  "_source_cost": copy.deepcopy(model["cost"]),
                  "_devin_pricing": {"basis": "public_list_price", "variant": model["variant"],
                                     "excludes": ["subscription allowance", "promotions",
                                                  "enterprise discounts", "account-specific billing"]},
                  "_price_status": "computable"}
        for field, value in model["cost"].items():
            record[RATE_FIELDS[field]] = value / 1_000_000
        result[key] = record
    return len(devin["models"]) - replaced, replaced


# Exact ID/name mapping manually verified in Grok Build CLI's advertised catalog.
# The public pricing section supplies rates, not a public xAI API model ID.
def parse_grok(raw):
    text = raw.decode("utf-8")
    sections = re.findall(
        r"^## Grok 4\.7 Fast pricing \(Cursor and Grok Build only\)\s*\n(.*?)(?=^## |\Z)",
        text, re.M | re.S)
    if len(sections) != 1:
        raise ValueError("grok: unique official Fast pricing section not found")
    section = sections[0]
    if ("not available on the public xAI API" not in section
            or "prompt exceeds 200k tokens" not in section):
        raise ValueError("grok: Fast product availability or context boundary changed")
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.strip().startswith("|")]
    if (len(rows) != 4 or rows[0] != ["Prompt tokens", "Input", "Cached input", "Output"]
            or len(rows[1]) != 4 or not all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1])):
        raise ValueError("grok: unrecognized Fast pricing table")
    costs = {}
    for row, label, suffix in ((rows[2], "Below 200k", ""),
                               (rows[3], "Above 200k", "_above_200k_tokens")):
        if len(row) != 4 or row[0] != label:
            raise ValueError("grok: missing or ambiguous context pricing row")
        for field, cell in zip(("input", "cache_read", "output"), row[1:]):
            match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?) / 1M", cell)
            if match is None or not valid_rate(float(match.group(1))):
                raise ValueError(f"grok: invalid USD/million price {cell!r}")
            costs[field + suffix] = float(match.group(1))
    return {"models": {"grok-4.7-build-fast": {"name": "Grok 4.7 Build Fast", "cost": costs}}}


def add_grok_records(result, grok):
    replaced = 0
    for model_id, model in sorted(grok["models"].items()):
        key = f"grok/{model_id}"
        replaced += key in result
        record = {"litellm_provider": "grok", "mode": "chat", "name": model["name"],
                  "_price_provenance": {"source": "grok", "source_key": model_id,
                                        "source_name": "Grok 4.7 Fast",
                                        "model_id_source": "Grok Build CLI advertised model catalog",
                                        "url": SOURCES["grok"],
                                        "source_unit": "USD per million tokens"},
                  "_source_cost": copy.deepcopy(model["cost"]),
                  "_grok_pricing": {"basis": "Grok Build public token list price",
                                    "public_xai_api_available": False,
                                    "variant": "fast",
                                    "long_context_threshold_input_tokens": 200000,
                                    "long_context_comparison": "greater_than",
                                    "long_context_applies_to": "all request tokens",
                                    "excludes": ["subscription allowance", "free tier",
                                                 "account-specific billing"]},
                  "_price_status": "computable"}
        for field in ("input", "cache_read", "output"):
            for suffix in ("", "_above_200k_tokens"):
                record[RATE_FIELDS[field] + suffix] = model["cost"][field + suffix] / 1_000_000
        result[key] = record
    return len(grok["models"]) - replaced, replaced


def fetch(name, url):
    last_error = None
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "model-prices-sync/1.0 (+https://github.com/jiayuqi7813/model-prices)",
                # xAI's explicit .md endpoint returns 404 for Accept: text/markdown.
                "Accept": ("text/html" if name == "cursor" else "text/markdown"
                           if name == "devin" else "*/*" if name == "grok" else "application/json"),
            })
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            data = (parse_cursor(raw) if name == "cursor" else parse_devin(raw)
                    if name == "devin" else parse_grok(raw) if name == "grok" else decode(raw))
            validate_source(name, data)
            return data, {"url": url, "fetched_at": timestamp(),
                          "sha256": hashlib.sha256(raw).hexdigest(),
                          "count": (len(data["models"]) if name in ("cursor", "devin", "grok") else len(data)
                                    if name == "litellm" else sum(
                                        len(provider["models"]) for provider in data.values())),
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
    elif name in ("cursor", "devin", "grok"):
        if not data.get("models") or not all(
                valid_rate(row["cost"].get("input")) and valid_rate(row["cost"].get("output"))
                for row in data["models"].values()):
            raise ValueError(f"{name}: no usable official token prices")
        if name == "grok":
            if set(data["models"]) != {"grok-4.7-build-fast"} or not all(
                    valid_rate(row["cost"].get(field + suffix))
                    for row in data["models"].values()
                    for field in ("input", "cache_read", "output")
                    for suffix in ("", "_above_200k_tokens")):
                raise ValueError("grok: missing model or context prices")
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


def build(litellm, modelsdev, source_meta, cursor, devin, grok):
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
    cursor_replaced = 0
    for model_id, model in sorted(cursor["models"].items()):
        key = f"cursor/{model_id}"
        if key in result:
            cursor_replaced += 1
        record = {"litellm_provider": "cursor", "mode": "chat", "name": model["name"],
                  "_price_provenance": {"source": "cursor", "source_key": model_id,
                                        "source_name": model["name"], "url": SOURCES["cursor"],
                                        "model_id_catalog_url": CURSOR_ID_CATALOG,
                                        "source_unit": "USD per million tokens"},
                  "_source_cost": copy.deepcopy(model["cost"]),
                  "_cursor_pricing": {"variant": model["variant"],
                                      "basis": "base list / additional usage estimate",
                                      "excludes": ["monthly subscription", "plan token surcharges",
                                                   "regional pricing", "conditional Max/Auto routing"]}}
        if model_id.startswith("composer-2.5"):
            record["_cursor_pricing"]["product_default_variant"] = "fast"
        fast_id = model_id + "-fast"
        if model_id == "grok-4.7-long":
            fast_id = "grok-4.7-fast-long"
        if fast_id in cursor["models"]:
            record["_cursor_pricing"]["fast_model_id"] = fast_id
        if model_id.startswith("grok-4.7"):
            record["_cursor_pricing"]["long_context_threshold_input_tokens"] = 256000
            record["_cursor_pricing"]["long_context_model_id"] = (
                "grok-4.7-fast-long" if "fast" in model_id else "grok-4.7-long")
            record["_cursor_pricing"]["applies_above_threshold"] = "long" in model_id
            if "long" in model_id:
                record["_cursor_pricing"]["short_context_model_id"] = (
                    "grok-4.7-fast" if "fast" in model_id else "grok-4.7")
        for field, value in model["cost"].items():
            record[RATE_FIELDS[field]] = value / 1_000_000
        record["_price_status"] = "computable"
        result[key] = record
    devin_added, devin_replaced = add_devin_records(result, devin)
    grok_added, grok_replaced = add_grok_records(result, grok)
    result["_meta"] = {"schema_version": 1, "generated_at": timestamp(),
                       "currency": "USD", "token_rate_unit": "USD per token",
                       "sources": source_meta,
                       "record_count": len(result), "modelsdev_added": added,
                       "modelsdev_existing_keys_skipped": skipped,
                       "devin_added": devin_added, "devin_replaced_aggregate_keys": devin_replaced,
                       "grok_added": grok_added, "grok_replaced_aggregate_keys": grok_replaced,
                       "cursor_added": len(cursor["models"]) - cursor_replaced,
                       "cursor_replaced_aggregate_keys": cursor_replaced,
                       "cursor_pricing_rows": cursor["pricing_rows"],
                       "cursor_unmapped_names": cursor["unmapped_names"]}
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
        raise ValueError("All required source metadata entries must be present")
    for name, details in meta["sources"].items():
        if (details.get("status") != "ok" or details.get("url") != SOURCES[name]
                or not isinstance(details.get("count"), int) or details["count"] <= 0
                or len(details.get("sha256", "")) != 64):
            raise ValueError(f"Invalid metadata for {name}")
    records = [(key, value) for key, value in data.items() if key != "_meta"]
    if not records or meta.get("record_count") != len(records):
        raise ValueError("Empty feed or record count mismatch")
    if data.get("grok/grok-4.7-build-fast", {}).get("_price_provenance", {}).get("source") != "grok":
        raise ValueError("Missing official Grok Build Fast record")
    usable = 0
    for key, record in records:
        if not isinstance(record, dict) or not record.get("_price_provenance"):
            raise ValueError(f"Missing record/provenance: {key}")
        expected = "computable" if computable(record) else "unknown"
        if record.get("_price_status") != expected:
            raise ValueError(f"Incorrect price status: {key}")
        if record["_price_provenance"].get("source") == "grok":
            for field in ("input_cost_per_token", "cache_read_input_token_cost", "output_cost_per_token"):
                if not all(valid_rate(record.get(field + suffix))
                           for suffix in ("", "_above_200k_tokens")):
                    raise ValueError(f"Missing Grok context price: {key}")
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
            # Nothing is written until all required sources fetch and validate.
            for name, url in SOURCES.items():
                data_by_source[name], meta[name] = fetch(name, url)
                print(f"Fetched {name}: {meta[name]['count']} source entries", flush=True)
            data = build(data_by_source["litellm"], data_by_source["models.dev"], meta,
                         data_by_source["cursor"], data_by_source["devin"], data_by_source["grok"])
            atomic_write(args.output, data)
        usable = validate_feed(data)
        print(f"Validated {len(data) - 1} records ({usable} computable): {args.output}")
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
