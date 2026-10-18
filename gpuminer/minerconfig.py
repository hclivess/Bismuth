"""TOML configuration for the GPU miners (doc/11 format, miner-local).

Both GPU miners read TOML: the CUDA pool miner ``optihash.py`` reads ``miner.toml``, the OpenCL miner
``opencl_alt/`` reads ``config.toml`` (plus an optional ``config_custom.toml`` overlay). Values are
coerced to each miner's schema (``str`` / ``int`` / ``list`` / ``intlist``), so an operator may write
``port = 5658`` or ``port = "5658"`` alike. Unknown keys are ignored.

A pre-TOML ``key=value`` file (``miner.txt`` / ``config.txt`` / ``config_custom.txt``) found without
its TOML counterpart is migrated once, the same way the node migrates its own config: the TOML is
written atomically only after it is verified to load to the identical settings, and the old file is
renamed to ``<name>.migrated``.

Stdlib only (``tomllib``; the ``tomli`` package on Python 3.10) so the miners stay standalone — copy this
file next to the miner script.
"""
import json
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


def _coerce(value, typ):
    """Coerce one config value (TOML-native or a legacy string) to its schema type."""
    if typ == "intlist":
        if isinstance(value, str):
            value = [v for v in value.split(",") if v.strip()]
        return [int(v) for v in value]
    if typ == "list":
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(v) for v in value]
    if typ == "int":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return int(value.strip().lower() == "true")
        return int(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def coerce(raw, schema):
    """Keep the schema's keys from ``raw``, each coerced to its type."""
    return {k: _coerce(v, schema[k]) for k, v in raw.items() if k in schema}


def load_toml(path, schema):
    with open(path, "rb") as f:
        return coerce(tomllib.load(f), schema)


def parse_legacy(path):
    """A pre-TOML ``key=value`` file -> {key: raw string}; blank lines and ``#`` comments skipped."""
    raw = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            raw[key.strip()] = value.strip()
    return raw


def _toml_value(value):
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)  # a JSON string literal is a valid TOML basic string


def build_toml(values, header=""):
    lines = [header] if header else []
    lines += ["%s = %s" % (k, _toml_value(v)) for k, v in values.items()]
    return "\n".join(lines) + "\n"


def migrate_legacy(txt_path, toml_path, schema):
    """Convert a legacy ``key=value`` file to TOML, verified identical; the source becomes ``.migrated``."""
    expected = coerce(parse_legacy(txt_path), schema)
    tmp = toml_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(build_toml(expected, "# migrated from %s" % os.path.basename(txt_path)))
    try:
        if load_toml(tmp, schema) != expected:
            raise ValueError("%s: the TOML conversion does not load identically; nothing written" % txt_path)
    except Exception:
        os.remove(tmp)
        raise
    os.replace(tmp, toml_path)
    os.replace(txt_path, txt_path + ".migrated")
    return expected


def load(directory, name, schema, legacy=None, custom=None, legacy_custom=None):
    """Load ``directory/name`` (migrating ``legacy`` first if needed), overlaid by ``custom`` if present."""
    def _path(n):
        return os.path.join(directory, n)

    for src, dst in ((legacy, name), (legacy_custom, custom)):
        if src and dst and not os.path.exists(_path(dst)) and os.path.exists(_path(src)):
            migrate_legacy(_path(src), _path(dst), schema)
    values = load_toml(_path(name), schema)
    if custom and os.path.exists(_path(custom)):
        values.update(load_toml(_path(custom), schema))
    return values
