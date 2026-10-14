# Build from source

Use Python 3 (standard library only):

```sh
python3 src/build_release.py --output dist
(cd src && python3 -m unittest test_packaging -q)
```

The build produces the offline `.desktop` installer, an allowlisted source ZIP, documentation, and SHA-256 checksums. Rebuilding version 0.4.13 produces the same installer tested on Steam Deck.

The README inside `src/` is part of the immutable installation payload. Public documentation is maintained in the two README files at the repository root. Change the version before modifying an installation payload that has already been released.
