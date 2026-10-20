from typing import Any, Dict, List, Optional

import httpx

from .config import get_settings


class DexScreenerClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base = (base_url or get_settings().dexscreener_base_url).rstrip("/")

    def _get(self, path: str):
        r = httpx.get(self.base + path, timeout=15)
        r.raise_for_status()
        return r.json()

    def latest_profiles(self) -> List[Dict[str, Any]]:
        data = self._get("/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    def token_pairs(self, chain: str, address: str) -> List[Dict[str, Any]]:
        data = self._get(f"/tokens/v1/{chain}/{address}")
        return data if isinstance(data, list) else []


def _first(urls: List[Dict[str, Any]], kind: str) -> Optional[str]:
    for u in urls or []:
        if (u.get("type") or u.get("label") or "").lower() == kind and u.get("url"):
            return u["url"]
    return None


def extract_lead(profile: Dict[str, Any], pairs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pull only fields DexScreener actually returned; absent data stays None."""
    if not pairs:
        return None
    best = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
    info = best.get("info") or {}
    base = best.get("baseToken") or {}
    links = profile.get("links") or []
    websites = info.get("websites") or []
    official = [u.get("url") for u in (links + (info.get("socials") or []) + websites) if u.get("url")]
    return {
        "official_links": list(dict.fromkeys(official)),
        "chain": profile["chainId"], "token_address": profile["tokenAddress"],
        "project_name": (base.get("name") or "").strip() or base.get("symbol"), "token_symbol": base.get("symbol"),
        "website": (websites[0].get("url") if websites else None) or _first(links, "website"),
        "telegram_link": _first(info.get("socials") or [], "telegram") or _first(links, "telegram"),
        "dexscreener_url": best.get("url"),
        "liquidity_usd": (best.get("liquidity") or {}).get("usd") or 0,
        "created_ms": best.get("pairCreatedAt"),
    }
