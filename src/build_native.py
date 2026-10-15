"""[Codex] Rebuild the bundled stable-C-ABI parser from pinned upstream sources."""
import argparse
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--javascript', required=True, type=Path)
    parser.add_argument('--zig', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    core, grammar = args.core.resolve(), args.javascript.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = output.parent / '.native-build-cache'
    common = ['-O2', '-DNDEBUG', '-fPIC', '-D_POSIX_C_SOURCE=200809L', '-D_DEFAULT_SOURCE',
              '-D_DARWIN_C_SOURCE', '-I' + str(core / 'lib/include'), '-I' + str(core / 'lib/src'),
              '-I' + str(grammar / 'src'), str(core / 'lib/src/lib.c'), str(grammar / 'src/parser.c'),
              str(grammar / 'src/scanner.c'), '-ffile-prefix-map=' + str(core.parent) + '=.']
    command = [str(args.zig.resolve()), 'cc', '-target', 'x86_64-linux-gnu.2.17', '-shared',
               '-Wl,--strip-all', *common, '-o', str(output)]
    environment = {'PATH': os.defpath, 'ZIG_GLOBAL_CACHE_DIR': str(cache / 'global'),
                   'ZIG_LOCAL_CACHE_DIR': str(cache / 'local')}
    subprocess.run(command, env=environment, check=True, timeout=90)


if __name__ == '__main__':
    main()
