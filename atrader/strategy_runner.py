"""Run restricted strategy experiments in a credential-free bounded subprocess."""
import ast
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def validate_source(source):
    if len(source.encode()) > 64000:
        raise ValueError('Strategy source exceeds 64 KiB')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name != 'math' for n in node.names):
            raise ValueError('Only math imports are allowed')
        if isinstance(node, ast.ImportFrom) and (node.module != 'math' or node.level or any(n.name.startswith('_') or n.name == '*' for n in node.names)):
            raise ValueError('Only explicit public math imports are allowed')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('Dunder introspection is forbidden')
        if isinstance(node, ast.Attribute):
            if node.attr.startswith('_') and not (isinstance(node.value, ast.Name) and node.value.id == 'self' and not node.attr.startswith('__')):
                raise ValueError(f'Private/introspection attribute forbidden: {node.attr} at line {node.lineno}')
            if isinstance(node.ctx, (ast.Store, ast.Del)) and not (isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'params'):
                raise ValueError('Only self.params attribute assignment is allowed')
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith('__') and node.name != '__init__':
                raise ValueError('Magic methods are forbidden')
            if node.decorator_list:
                raise ValueError('Decorators are forbidden')
        if isinstance(node, ast.ClassDef) and (node.bases or node.keywords or node.decorator_list):
            raise ValueError('Class inheritance and metaclasses are forbidden')
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.Delete, ast.With, ast.AsyncWith)):
            raise ValueError('Unsupported strategy construct')
    if not any(isinstance(n, ast.ClassDef) and n.name == 'Strategy' for n in tree.body):
        raise ValueError('Strategy class required')
    return tree


def load_restricted(source, compiled=None):
    compiled = compiled if compiled is not None else compile(validate_source(source), '<strategy>', 'exec')
    import builtins
    allowed = ('abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'float', 'int', 'isinstance', 'len',
               'list', 'max', 'min', 'range', 'round', 'set', 'sorted', 'str', 'sum', 'tuple', 'zip',
               'ValueError', 'TypeError', 'Exception', '__build_class__')
    safe = {name: getattr(builtins, name) for name in allowed}
    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name != 'math' or level:
            raise ValueError('Import denied')
        return math
    safe['__import__'] = restricted_import
    namespace = {'__builtins__': safe, '__name__': 'candidate_strategy'}
    exec(compiled, namespace)
    strategy = namespace['Strategy']()
    if not callable(getattr(strategy, 'rank_assets', None)):
        raise ValueError('Strategy.rank_assets(history, positions) required')
    return strategy


def run_strategy(strategy_path, request):
    source = Path(strategy_path).read_text(encoding='utf-8')
    validate_source(source)
    payload = dict(request, source=source)
    worker = Path(__file__).with_name('worker.py').resolve()
    with tempfile.TemporaryDirectory(prefix='atrader-worker-') as temporary:
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '0', 'HOME': temporary}
        try:
            process = subprocess.run([sys.executable, '-I', str(worker)], input=json.dumps(payload),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temporary,
                env=environment, timeout=45)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Strategy worker exceeded 45 second limit') from exc
    if process.returncode:
        raise RuntimeError('Strategy worker failed: ' + process.stderr[-1500:])
    if len(process.stdout) > 4_000_000:
        raise RuntimeError('Strategy output too large')
    result = json.loads(process.stdout)
    if 'error' in result:
        raise ValueError(result['error'])
    result['strategy_source_hash'] = hashlib.sha256(source.encode()).hexdigest()
    return result
