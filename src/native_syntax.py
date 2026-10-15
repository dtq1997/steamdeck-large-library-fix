"""[Codex] Isolated native JavaScript parser using Tree-sitter's stable C ABI."""
import ctypes as C
import json
import os
import resource
import sys


class Node(C.Structure):
    _fields_ = [('context', C.c_uint32 * 4), ('id', C.c_void_p), ('tree', C.c_void_p)]


def library(path):
    lib = C.CDLL(path)
    signatures = {
        'tree_sitter_javascript': (C.c_void_p, []),
        'ts_parser_new': (C.c_void_p, []),
        'ts_parser_set_language': (C.c_bool, [C.c_void_p, C.c_void_p]),
        'ts_parser_set_timeout_micros': (None, [C.c_void_p, C.c_uint64]),
        'ts_parser_parse_string': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_char_p, C.c_uint32]),
        'ts_parser_delete': (None, [C.c_void_p]),
        'ts_tree_root_node': (Node, [C.c_void_p]),
        'ts_tree_delete': (None, [C.c_void_p]),
        'ts_node_has_error': (C.c_bool, [Node]),
        'ts_node_is_null': (C.c_bool, [Node]),
        'ts_node_type': (C.c_char_p, [Node]),
        'ts_node_parent': (Node, [Node]),
        'ts_node_start_byte': (C.c_uint32, [Node]),
        'ts_node_end_byte': (C.c_uint32, [Node]),
        'ts_node_descendant_for_byte_range': (Node, [Node, C.c_uint32, C.c_uint32]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    return lib


def code_position(lib, root, position):
    node = lib.ts_node_descendant_for_byte_range(root, position, position)
    for _ in range(512):
        if lib.ts_node_is_null(node):
            return True
        kind = lib.ts_node_type(node)
        if kind in {b'comment', b'string', b'template_string', b'regex'}:
            return False
        if kind == b'template_substitution':
            return True
        node = lib.ts_node_parent(node)
    return False


def literal_position(lib, root, item, source):
    start, end = item['start'], item['end']
    node = lib.ts_node_descendant_for_byte_range(root, start, end)
    return (lib.ts_node_type(node) == b'string' and lib.ts_node_start_byte(node) == start
            and lib.ts_node_end_byte(node) == end
            and source[start:end] == item['raw'].encode('utf-8'))


def inspect(lib, request):
    source = request['source'].encode('utf-8')
    if len(source) > 32 * 1024 * 1024:
        raise ValueError('Client source exceeds supported size')
    if any(not isinstance(p, int) or p < 0 or p >= len(source) for p in request['offsets']):
        raise ValueError('Invalid requested source position')
    if any(not 0 <= item['start'] < item['end'] <= len(source) for item in request['budget_candidates']):
        raise ValueError('Invalid requested string range')
    parser, tree = lib.ts_parser_new(), None
    try:
        if not parser or not lib.ts_parser_set_language(parser, lib.tree_sitter_javascript()):
            raise ValueError('Native parser ABI mismatch')
        lib.ts_parser_set_timeout_micros(parser, 10_000_000)
        tree = lib.ts_parser_parse_string(parser, None, source, len(source))
        if not tree:
            raise ValueError('Native parser timed out')
        root = lib.ts_tree_root_node(tree)
        if lib.ts_node_has_error(root):
            raise ValueError('Client source contains unsupported or invalid syntax')
        valid = [position for position in request['offsets'] if code_position(lib, root, position)]
        budgets = [index for index, item in enumerate(request['budget_candidates'])
                   if literal_position(lib, root, item, source)]
        return {'locations': valid, 'budgets': budgets}
    finally:
        if tree:
            lib.ts_tree_delete(tree)
        if parser:
            lib.ts_parser_delete(parser)


def main():
    os.nice(10)
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    try:
        result = inspect(library(sys.argv[1]), json.load(sys.stdin))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
