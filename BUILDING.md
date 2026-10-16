# Build from source

Use Python 3 (standard library only):

```sh
python3 src/build_release.py --output dist
(cd src && python3 -m unittest test_packaging -q)
```

The build produces the offline `.desktop` installer, an allowlisted source ZIP, documentation, and SHA-256 checksums. Version 0.4.19 includes its own bounded JavaScript syntax checker; users do not need Node.js. Third-party versions, digests, and licenses are in src/vendor.json and src/THIRD-PARTY-NOTICES.txt.

The README inside `src/` is part of the immutable installation payload. Public documentation is maintained in the two README files at the repository root. Change the version before modifying an installation payload that has already been released.

The full-file parser uses Tree-sitter's C ABI through Python ctypes, without a dependency on a particular Python extension ABI. Rebuilding that library is optional when rebuilding the installer. Download the pinned Tree-sitter 0.25.2 and JavaScript grammar 0.25.0 source archives listed with SHA-256 digests in `src/vendor.json`, then use Zig 0.17.0:

```sh
python3 src/build_native.py --core /path/tree-sitter-0.25.2 --javascript /path/tree-sitter-javascript-0.25.0 --zig /path/zig --output src/libsyntax-linux-x86_64.so
```

Run `python3 -m unittest test_compat test_packaging -q` from `src/` on a native Deck to include parser and transaction checks. Private full-client fixtures are optional and are never part of a release. macOS development uses `LLF_DEVELOPMENT_QJS` and `LLF_DEVELOPMENT_LIBRARY` for equivalent local builds; Linux releases always use the bundled binaries.
