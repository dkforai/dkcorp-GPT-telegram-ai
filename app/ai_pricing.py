from __future__ import annotations

from dataclasses import dataclass


USD_TO_IDR = 17_500


@dataclass(frozen=True)
class PriceEstimate:
    usd: float | None
    idr: int | None
    pricing_note: str


def estimate_completion_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> PriceEstimate:
    price = _price_per_million(provider, model)
    if price is None:
        return PriceEstimate(None, None, "Harga model belum tersedia")
    input_price, output_price, note = price
    usd = (max(0, input_tokens) * input_price + max(0, output_tokens) * output_price) / 1_000_000
    return PriceEstimate(usd, int(round(usd * USD_TO_IDR)), note)


def _price_per_million(provider: str, model: str) -> tuple[float, float, str] | None:
    name = model.casefold()
    provider = provider.casefold()
    if provider == "openai":
        if name.startswith(("gpt-5.2-pro", "gpt-5-pro")):
            return (21.0, 168.0, "Estimasi harga publik per 1M token")
        if name.startswith(("gpt-5.2", "gpt-5.2-chat-latest")):
            return (1.75, 14.0, "Estimasi harga publik per 1M token")
        if name.startswith(("gpt-5.1", "gpt-5.1-chat-latest", "gpt-5-")) or name == "gpt-5":
            return (1.25, 10.0, "Estimasi harga publik per 1M token")
        if "mini" in name:
            return (0.25, 2.0, "Estimasi harga publik per 1M token")
    if provider == "anthropic":
        if "opus" in name:
            return (15.0, 75.0, "Estimasi harga publik per 1M token")
        if "sonnet" in name:
            return (3.0, 15.0, "Estimasi harga publik per 1M token")
        if "haiku" in name:
            return ((0.80, 4.0, "Estimasi harga publik per 1M token") if "3-5" in name or "3.5" in name else (0.25, 1.25, "Estimasi harga publik per 1M token"))
    if provider == "deepseek":
        if "v4-pro" in name or "v4_pro" in name:
            return (0.435, 0.87, "Estimasi cache miss per 1M token")
        if "v4-flash" in name or "v4_flash" in name:
            return (0.14, 0.28, "Estimasi cache miss per 1M token")
        if "reasoner" in name:
            return (0.55, 2.19, "Estimasi cache miss per 1M token")
        return (0.27, 1.10, "Estimasi cache miss per 1M token")
    if provider == "gemini":
        if "flash" in name:
            return (0.75, 3.75, "Estimasi harga publik per 1M token")
        if "pro" in name:
            return (1.25, 10.0, "Estimasi harga publik per 1M token")
        return (1.0, 5.0, "Estimasi harga publik per 1M token")
    return None
