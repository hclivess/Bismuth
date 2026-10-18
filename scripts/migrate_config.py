#!/usr/bin/env python3
"""Migrate a key=value config.txt to config.toml (doc/11).

The node does this itself on startup (options.Get.read -> options.migrate_legacy) whenever a config.txt
exists and config.toml does not; this script is the same conversion, run by hand. The emitted TOML holds
only the keys the operator set, and is verified to load to the identical Config before it is written
(atomic: temp + rename). The source is renamed to <name>.migrated.

Usage:
    python3 scripts/migrate_config.py [--in config.txt] [--out config.toml] [--check] [--force] [--custom]
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import options  # noqa: E402

# re-exported for callers/tests that used the script's helpers
build_toml = options.build_toml
_present_keys = options._present_keys


def main(argv=None):
    ap = argparse.ArgumentParser(description="Migrate config.txt -> config.toml")
    ap.add_argument("--in", dest="src", default="config.txt", help="key=value source (default: config.txt)")
    ap.add_argument("--out", dest="dst", default="config.toml", help="TOML destination (default: config.toml)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing destination")
    ap.add_argument("--check", action="store_true", help="verify the conversion only; write nothing")
    ap.add_argument("--custom", action="store_true",
                    help="also migrate config_custom.txt -> config_custom.toml if present")
    args = ap.parse_args(argv)

    pairs = [(args.src, args.dst)]
    if args.custom and os.path.exists("config_custom.txt"):
        pairs.append(("config_custom.txt", "config_custom.toml"))

    for src, dst in pairs:
        if not os.path.exists(src):
            print("skip: %s does not exist" % src)
            continue
        if args.check:
            tmp = dst + ".check"
            try:
                options.migrate_legacy(src, tmp, keep=True)
            except ValueError as e:
                print("ABORT: %s" % e)
                return 2
            os.replace(src + ".migrated", src)      # --check leaves the source exactly where it was
            os.remove(tmp)
            print("OK (--check): %s would migrate to %s with an identical Config (%d operator keys)"
                  % (src, dst, len(options._present_keys(src))))
            continue
        if os.path.exists(dst) and not args.force:
            print("refuse: %s already exists (use --force to overwrite). %s left intact." % (dst, src))
            return 1
        try:
            n = options.migrate_legacy(src, dst, keep=True)
        except ValueError as e:
            print("ABORT: %s" % e)
            return 2
        print("wrote %s (%d operator keys); %s renamed to %s.migrated" % (dst, n, src, src))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
