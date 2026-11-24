"""Первоисточник статистики: World Bank API (бесплатно, без ключа)."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request

from .models import Claim, ClaimType, Snippet

API = "https://api.worldbank.org/v2"

# ключевые слова (английский пайплайн) -> индикатор World Bank
INDICATORS: dict[str, tuple[str, str]] = {
    "remittance": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "remittances": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "money transfer": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "money transfers": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "money remittance": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "migrant remittance": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "transfers to": ("BX.TRF.PWKR.CD.DT", "personal remittances received"),
    "population": ("SP.POP.TOTL", "population"),
    "gdp": ("NY.GDP.MKTP.CD", "GDP"),
    "inflation": ("FP.CPI.TOTL.ZG", "inflation, consumer prices"),
    "unemployment": ("SL.UEM.TOTL.ZS", "unemployment rate"),
    "military expenditure": ("MS.MIL.XPND.CD", "military expenditure"),
    "refugee": ("SM.POP.REFG", "refugee population"),
    "life expectancy": ("SP.DYN.LE00.IN", "life expectancy"),
    "electricity": ("EG.ELC.ACCS.ZS", "access to electricity"),
    "internet": ("IT.NET.USER.ZS", "internet users"),
}

COUNTRY_HINTS = {
    "mexico": "MEX", "russia": "RUS", "ukraine": "UKR", "united states": "USA",
    "usa": "USA", "china": "CHN", "germany": "DEU", "france": "FRA",
    "india": "IND", "brazil": "BRA", "turkey": "TUR", "türkiye": "TUR",
    "poland": "POL", "belarus": "BLR", "kazakhstan": "KAZ", "georgia": "GEO",
    "armenia": "ARM", "azerbaijan": "AZE", "israel": "ISR", "iran": "IRN",
    "japan": "JPN", "united kingdom": "GBR", "canada": "CAN", "italy": "ITA",
    "spain": "ESP", "libya": "LBY", "syria": "SYR", "egypt": "EGY",
}

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUM = re.compile(r"\d[\d\s.,]*")


class WorldBankStats:
    def __init__(self, *, timeout: float = 15.0):
        self.timeout = timeout
        self._cache: dict[tuple, Snippet | None] = {}

    def _fetch(self, url: str) -> dict | None:
        request = urllib.request.Request(url, headers={"User-Agent": "tgnews/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"[wb] {type(exc).__name__}: {str(exc)[:100]}")
            return None

    def _country(self, claim: Claim) -> str | None:
        text = " ".join([claim.text, *claim.entities]).lower()
        for name, code in sorted(COUNTRY_HINTS.items(), key=lambda item: -len(item[0])):
            if name in text:
                return code
        return None

    def _indicator(self, claim: Claim) -> tuple[str, str] | None:
        text = " ".join([claim.text, *claim.entities]).lower()
        for keyword, value in INDICATORS.items():
            if keyword in text:
                return value
        return None

    async def lookup(self, claim: Claim) -> list[Snippet]:
        """Сниппеты с точными значениями по годам из утверждения."""
        if claim.ctype not in (ClaimType.NUMERIC, ClaimType.DATE):
            return []
        country = self._country(claim)
        indicator = self._indicator(claim)
        years = _YEAR.findall(claim.text)
        if not country or not indicator or not years:
            return []
        code, name = indicator
        snippets: list[Snippet] = []
        for year_value in years[:3]:
            key = (country, code, year_value)
            if key not in self._cache:
                url = (f"{API}/country/{country}/indicator/{code}"
                       f"?format=json&date={year_value}&per_page=1")
                data = await asyncio.to_thread(self._fetch, url)
                snippet = None
                try:
                    row = data[1][0]
                    value = row.get("value")
                    if value is not None:
                        snippet = Snippet(
                            url=f"https://data.worldbank.org/indicator/{code}?locations={country}",
                            domain="worldbank.org",
                            title=f"World Bank: {name}",
                            quote=(f"{row.get('country', {}).get('value', country)} — {name} "
                                   f"in {year_value}: {value:,.0f}"),
                        )
                except Exception:
                    snippet = None
                self._cache[key] = snippet
            if self._cache[key]:
                snippets.append(self._cache[key])
        return snippets
