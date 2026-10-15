"""Lossless removal of repeated JSON field names from model evidence."""

from collections import Counter


FORMAT = "shared-record-fields-v1"
INSTRUCTIONS = (
    "mandatory_evidence uses shared-record-fields-v1, a lossless JSON encoding. "
    "record_fields maps each record ID to its ordered field names. An object "
    "{\"$record\":[ID,value1,value2,...]} is an ordinary object whose field names "
    "are record_fields[ID]; decode nested records the same way. data contains the "
    "complete evidence, with every bout and exact value retained."
)


def pack_evidence(evidence):
    counts = Counter()

    def count(value):
        if isinstance(value, dict):
            counts[tuple(value)] += 1
            for child in value.values():
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)

    count(evidence)
    schemas = {keys: f"r{index}" for index, (keys, n) in enumerate(counts.items())
               if n >= 3 and len(keys) >= 3}

    def pack(value):
        if isinstance(value, dict):
            keys = tuple(value)
            if keys in schemas:
                return {"$record": [schemas[keys], *(pack(value[k]) for k in keys)]}
            return {k: pack(v) for k, v in value.items()}
        if isinstance(value, list):
            return [pack(v) for v in value]
        return value

    return {"encoding": FORMAT, "record_fields": {v: list(k) for k, v in schemas.items()}, "data": pack(evidence)}


def unpack_evidence(payload):
    """Expand archived compact inputs for inspection and offline verification."""
    if payload.get("encoding") != FORMAT:
        return payload

    def unpack(value):
        if isinstance(value, dict):
            if set(value) == {"$record"}:
                record_id, *values = value["$record"]
                return {k: unpack(v) for k, v in zip(payload["record_fields"][record_id], values, strict=True)}
            return {k: unpack(v) for k, v in value.items()}
        if isinstance(value, list):
            return [unpack(v) for v in value]
        return value

    return unpack(payload["data"])
