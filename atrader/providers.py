"""Public provider identity; never expose credentials or invent model versions."""
import hashlib
import re
from urllib.parse import urlsplit


def model_configuration_id(cfg):
    identity = cfg.get('llm_base_url', '').rstrip('/') + '\n' + cfg.get('llm_model', '')
    return hashlib.sha256(identity.encode()).hexdigest()


def model_endpoint(cfg):
    try:
        endpoint = urlsplit(cfg.get('llm_base_url', ''))
        host = endpoint.hostname or ''
        port = ':' + str(endpoint.port) if endpoint.port else ''
        return endpoint.scheme + '://' + host + port if host else ''
    except ValueError:
        return ''


def provider_details(cfg, history):
    origin = model_endpoint(cfg)
    host = urlsplit(origin).hostname or ''
    provider = {'api.deepseek.com':'DeepSeek', 'api.openai.com':'OpenAI',
                'openrouter.ai':'OpenRouter'}.get(host, host or '未配置')
    configuration_id = model_configuration_id(cfg)
    last = next((item for item in reversed(history.get('generations', []))
                 if item.get('model_configuration_id') == configuration_id and item.get('model_reported')), {})
    reported = last.get('model_reported')
    # A dated/specific identifier is evidence; a rolling alias is not a version.
    version = last.get('model_version_reported')
    if not version and reported:
        match = re.search(r'(?<!\d)(?:20\d{2}[-_]\d{2}[-_]\d{2}|v\d+(?:\.\d+)+)(?!\d)', reported, re.I)
        version = match.group(0) if match else None
    return {
        'paper_account': {'provider':'ATrader Local Paper' if cfg.get('execution_provider') == 'local_paper' else 'Alpaca Paper Trading',
            'adapter':'local_paper' if cfg.get('execution_provider') == 'local_paper' else 'alpaca',
            'mode':'模拟交易', 'currency':'USD',
            'research_data_provider': {'alpaca':'Alpaca IEX（现金分红与拆股调整）','yahoo':'Yahoo Finance（冻结研究样本）','synthetic':'合成研究样本'}.get(cfg.get('market_data_source'),cfg.get('market_data_source')),
            'market_data_provider': 'Alpaca IEX' if cfg.get('execution_provider') != 'local_paper' else {'yahoo':'Yahoo Finance（公开行情）', 'synthetic':'合成行情'}.get(cfg.get('market_data_source'), cfg.get('market_data_source'))},
        'model': {'provider':provider, 'endpoint':origin, 'configured_model':cfg.get('llm_model'),
            'reported_model':reported, 'version':version, 'system_fingerprint':last.get('system_fingerprint'),
            'observed_at':last.get('timestamp'), 'key_configured':bool(cfg.get('llm_api_key') and cfg.get('llm_api_key') != 'your_deepseek_api_key_here')}
    }
