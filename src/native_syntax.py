"""[Codex] Isolated native JavaScript parser using Tree-sitter's stable C ABI."""
import ctypes as C
import json
import os
import re
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
        'ts_node_named_child_count': (C.c_uint32, [Node]),
        'ts_node_named_child': (Node, [Node, C.c_uint32]),
        'ts_node_child_by_field_name': (Node, [Node, C.c_char_p, C.c_uint32]),
        'ts_node_descendant_for_byte_range': (Node, [Node, C.c_uint32, C.c_uint32]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    return lib


def field(lib, node, name):
    return lib.ts_node_child_by_field_name(node, name, len(name))


def names_in(lib, node, source):
    raw = source[lib.ts_node_start_byte(node):lib.ts_node_end_byte(node)].decode()
    raw = re.sub(r'\\u(?:\{([0-9a-fA-F]+)\}|([0-9a-fA-F]{4}))',
                 lambda match: chr(int(match[1] or match[2], 16)), raw)
    return set(re.findall(r'[A-Za-z_$][\w$]*', raw))


def direct_eval(lib, node, source):
    callee = field(lib, node, b'function')
    for _ in range(8):
        if lib.ts_node_type(callee) != b'parenthesized_expression':
            return lib.ts_node_type(callee) == b'identifier' and names_in(lib, callee, source) == {'eval'}
        if lib.ts_node_named_child_count(callee) != 1:
            return False
        callee = lib.ts_node_named_child(callee, 0)
    return True


def binding_names(lib, root, source):
    """Conservatively collect this outer scope's bindings, without entering functions."""
    pending, names = [root], set()
    declarations = {b'function_declaration', b'generator_function_declaration', b'class_declaration'}
    functions = {b'function_expression', b'generator_function', b'arrow_function', b'method_definition', b'class'}
    if lib.ts_node_type(root) in {b'formal_parameters', b'identifier', b'array_pattern', b'object_pattern'}:
        return names_in(lib, root, source)
    while pending:
        node = pending.pop()
        kind = lib.ts_node_type(node)
        if kind in declarations or kind == b'variable_declarator':
            name = field(lib, node, b'name')
            names.update(names_in(lib, name, source))
            continue
        if kind in functions:
            continue
        if kind == b'catch_clause':
            parameter = field(lib, node, b'parameter')
            names.update(names_in(lib, parameter, source))
        if kind == b'with_statement' or (kind == b'call_expression' and direct_eval(lib, node, source)):
            names.update({'JSON', 'Array', 'Promise', 'setTimeout', 'clearTimeout'})
        pending.extend(lib.ts_node_named_child(node, i) for i in range(lib.ts_node_named_child_count(node)))
    return names


def scope_names(lib, node, source, cache):
    key = (lib.ts_node_start_byte(node), lib.ts_node_end_byte(node), lib.ts_node_type(node))
    if key not in cache:
        cache[key] = binding_names(lib, node, source)
    return cache[key]


def local_unit(lib, root, position, source, cache):
    node = lib.ts_node_descendant_for_byte_range(root, position, position)
    if lib.ts_node_type(node) != b'property_identifier':
        return None
    method = lib.ts_node_parent(node)
    if lib.ts_node_type(method) != b'method_definition':
        return None
    method_name = source[lib.ts_node_start_byte(node):lib.ts_node_end_byte(node)]
    if len(method_name) > 128:
        return None
    parent, factory, outer = lib.ts_node_parent(method), None, set()
    for _ in range(512):
        if lib.ts_node_is_null(parent):
            break
        kind = lib.ts_node_type(parent)
        if kind in {b'arrow_function', b'function_expression', b'function_declaration', b'method_definition'}:
            if factory is None:
                factory = parent
            else:
                name = field(lib, parent, b'name')
                if not lib.ts_node_is_null(name):
                    outer.update(names_in(lib, name, source))
                parameters = field(lib, parent, b'parameters')
                if lib.ts_node_is_null(parameters):
                    parameters = field(lib, parent, b'parameter')
                outer.update(scope_names(lib, parameters, source, cache))
                outer.update(scope_names(lib, field(lib, parent, b'body'), source, cache))
        parent = lib.ts_node_parent(parent)
    if factory is None:
        return None
    outer.update(scope_names(lib, root, source, cache))
    owner = lib.ts_node_parent(factory)
    for _ in range(8):
        if lib.ts_node_type(owner) != b'parenthesized_expression':
            break
        owner = lib.ts_node_parent(owner)
    key = field(lib, owner, b'key') if lib.ts_node_type(owner) == b'pair' else None
    module = source[lib.ts_node_start_byte(key):lib.ts_node_end_byte(key)].decode() if key is not None else ''
    return {'name': method_name.decode(), 'start': lib.ts_node_start_byte(method), 'end': lib.ts_node_end_byte(method),
            'context_start': lib.ts_node_start_byte(factory), 'context_end': lib.ts_node_end_byte(factory),
            'module': module, 'factory_kind': lib.ts_node_type(factory).decode(),
            'outer_bindings': sorted(outer)}


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
            and lib.ts_node_type(lib.ts_node_parent(node)) != b'expression_statement'
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
        cache = {}
        units = [local_unit(lib, root, position, source, cache) for position in request.get('methods', [])]
        return {'locations': valid, 'budgets': budgets, 'units': [unit for unit in units if unit]}
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
        print(json.dumps({'error': str(error) or type(error).__name__}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
