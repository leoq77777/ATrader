"""Trusted child entry point; strategy receives no credentials or project handles."""
import json
import sys
from pathlib import Path
import resource

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atrader.strategy_runner import load_restricted, validate_source
from atrader.backtest import evaluate
from atrader.portfolio import allocate

resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
if sys.platform.startswith('linux'):
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

try:
    request = json.load(sys.stdin)
    compiled = compile(validate_source(request['source']), '<strategy>', 'exec')
    factory = lambda: load_restricted(request['source'], compiled)
    strategy = factory()
    if request['operation'] == 'decision':
        signals = strategy.rank_assets(request['history'], request['positions'])
        result = allocate(signals, request['positions'], request['config'])
    else:
        result = evaluate(factory, request['market'], request['config'], request.get('audit', False), request.get('layout'))
    result['strategy_name'] = str(strategy.name)[:150]
    result['strategy_description'] = str(getattr(strategy, 'description', ''))[:1000]
    result['strategy_params'] = getattr(strategy, 'params', {})
    encoded = json.dumps(result, allow_nan=False)
    if len(encoded) > 4_000_000:
        raise ValueError('Strategy result too large')
    sys.stdout.write(encoded)
except Exception as exc:
    sys.stdout.write(json.dumps({'error': f'{type(exc).__name__}: {str(exc)[:1500]}'}))
