from .panels import trading_rows


def process_rows(data, selected, width, wrap):
    return trading_rows(data, data.get("_view", "processes"), width, wrap)
