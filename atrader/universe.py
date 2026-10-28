"""Fixed initial scope: US-listed, unlevered sector equity ETFs; USD, long-only."""
SECTORS = {
    'XLB': '原材料', 'XLC': '通信服务', 'XLE': '能源', 'XLF': '金融',
    'XLI': '工业', 'XLK': '信息技术', 'XLP': '必需消费', 'XLRE': '房地产',
    'XLU': '公用事业', 'XLV': '医疗保健', 'XLY': '可选消费',
}
BENCHMARK = 'SPY'
UNIVERSE_VERSION = 'us_sector_equity_v1'


def validate_universe(symbols):
    if not symbols or len(set(symbols)) != len(symbols) or not set(symbols) <= set(SECTORS):
        raise ValueError('Universe must be a nonempty, unique subset of the fixed unlevered US sector ETF pool')
    return list(symbols)
