"""Every structured-output schema a section sends to Claude must follow the API's rules for structured
outputs: additionalProperties false on every object, every property required, and no constraint
keywords the API does not support. The site sends schemas as written, so a bad one would fail at
run time with a 400 from the API."""
import importlib

import pytest

from studio.app import FEATURES

UNSUPPORTED = {"minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "multipleOf", "maxItems", "uniqueItems", "minProperties", "maxProperties", "patternProperties"}


def _schemas():
    found = []
    for name in FEATURES:
        module = importlib.import_module(f"studio.features.{name}")
        for attr, value in vars(module).items():
            if attr.endswith("_SCHEMA") and isinstance(value, dict) and value.get("type") == "object":
                found.append((f"{name}.{attr}", value))
    return found


def _problems(schema, where):
    out = []
    if isinstance(schema, dict):
        bad = UNSUPPORTED & set(schema)
        if bad:
            out.append(f"{where}: unsupported keywords {sorted(bad)}")
        if "minItems" in schema and schema["minItems"] not in (0, 1):
            out.append(f"{where}: minItems above 1 is not supported")
        if schema.get("type") == "object":
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is not False:
                out.append(f"{where}: objects need additionalProperties false")
            if set(schema.get("required", [])) != set(props):
                out.append(f"{where}: every property must be required (missing {sorted(set(props) - set(schema.get('required', [])))})")
            for key, sub in props.items():
                out += _problems(sub, f"{where}.{key}")
        if "items" in schema:
            out += _problems(schema["items"], f"{where}[]")
        for key in ("anyOf", "allOf"):
            for i, sub in enumerate(schema.get(key, [])):
                out += _problems(sub, f"{where}.{key}[{i}]")
    return out


SCHEMAS = _schemas()


def test_sections_define_schemas():
    assert len(SCHEMAS) >= 5, [n for n, _ in SCHEMAS]


@pytest.mark.parametrize("name,schema", SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_schema_follows_structured_output_rules(name, schema):
    assert not _problems(schema, name)
