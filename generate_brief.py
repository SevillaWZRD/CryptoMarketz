#!/usr/bin/env python3
"""
ZIMR Capital - Daily Market Brief Generator

Fetches live market data (CoinGecko), Fear & Greed (alternative.me) and news
(CoinDesk / Cointelegraph RSS), then has Claude write a prose brief.

Writes: data/marketbrief.json

Exit codes:
  0 - brief regenerated successfully
  1 - failed, existing brief left in place (Action turns RED so you get notified)
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
# NOTE: claude-sonnet-4-20250514 was RETIRED 2026-06-15. Never pin a dated
# snapshot here again. Use the alias so minor version bumps are automatic.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

TODAY = str(date.today())
NOW_ISO = datetime.now(timezone.utc).isoformat()

api_key = os.environ.get("CLAUDE_API_KEY")
if not api_key:
    print("❌ CLAUDE_API_KEY not set. Check your repo secrets.")
    sys.exit(1)


def die(msg):
    """Fail loudly. Existing brief stays live, but the Action goes red."""
    print(f"❌ {msg}")
    print("   Existing brief left untouched. Fix and re-run.")
    sys.exit(1)


# ── HTTP helper with retries ──────────────────────────────────────────────
def fetch(url, retries=3, backoff=5):
    """GET with retry on 429/5xx. Raises after final attempt."""
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = backoff * attempt
                print(f"   ⏳ HTTP {e.code} on {url[:60]}, retry in {wait}s "
                      f"({attempt}/{retries})")
                time.sleep(wait)
            else:
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"   ⏳ Network error, retry in {wait}s ({attempt}/{retries})")
                time.sleep(wait)
            else:
                raise
    raise last_err


# ── 1. CoinGecko: prices, dominance, global mcap ──────────────────────────
print("📊 Fetching CoinGecko data...")

COINS = ("bitcoin,ethereum,solana,binancecoin,ripple,cardano,"
         "avalanche-2,chainlink,polkadot,uniswap")

SYMBOLS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "binancecoin": "BNB",
    "ripple": "XRP", "cardano": "ADA", "avalanche-2": "AVAX",
    "chainlink": "LINK", "polkadot": "DOT", "uniswap": "UNI",
}

try:
    market_data = json.loads(fetch(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={COINS}&order=market_cap_desc"
        "&per_page=10&page=1&sparkline=false&price_change_percentage=1h,24h,7d"
    ))
except Exception as e:
    die(f"CoinGecko markets endpoint failed: {e}")

if not market_data:
    die("CoinGecko returned an empty market list.")

# Never trust list position for BTC. Look it up by id.
btc = next((c for c in market_data if c["id"] == "bitcoin"), None)
if not btc:
    die("Bitcoin missing from CoinGecko response. Aborting rather than "
        "publishing a brief with no BTC anchor.")

btc_price = btc["current_price"]

try:
    global_data = json.loads(fetch("https://api.coingecko.com/api/v3/global"))["data"]
except Exception as e:
    die(f"CoinGecko global endpoint failed: {e}")

btc_dom = round(global_data["market_cap_percentage"]["btc"], 1)
eth_dom = round(global_data["market_cap_percentage"]["eth"], 1)
total_mcap = round(global_data["total_market_cap"]["usd"] / 1e12, 2)
total_vol = round(global_data["total_volume"]["usd"] / 1e9, 1)
mcap_chg = round(global_data.get("market_cap_change_percentage_24h_usd", 0), 2)

price_lines = []
for c in market_data:
    sym = SYMBOLS.get(c["id"], c["symbol"].upper())
    p = c["current_price"]
    h1 = round(c.get("price_change_percentage_1h_in_currency") or 0, 2)
    h24 = round(c.get("price_change_percentage_24h") or 0, 2)
    h7d = round(c.get("price_change_percentage_7d_in_currency") or 0, 2)
    vol = round(c["total_volume"] / 1e9, 2)
    price_lines.append(
        f"{sym}/USD: ${p:,} | 1h {h1:+.2f}% | 24h {h24:+.2f}% "
        f"| 7d {h7d:+.2f}% | vol ${vol}B"
    )

sorted_24h = sorted(market_data, key=lambda c: c.get("price_change_percentage_24h") or 0)


def fmt(c):
    return (f"{SYMBOLS.get(c['id'], c['symbol'].upper())} "
            f"{round(c.get('price_change_percentage_24h') or 0, 2):+.2f}%")


losers = [fmt(c) for c in sorted_24h[:3]]
gainers = [fmt(c) for c in sorted_24h[-3:][::-1]]

market_block = "\n".join(price_lines)
print(f"✅ {len(market_data)} coins | BTC ${btc_price:,} | dom {btc_dom}% "
      f"| mcap ${total_mcap}T")

# ── 2. Funding / OI ───────────────────────────────────────────────────────
funding_block = "Not available (API blocked from GitHub Actions servers)"
oi_block = "Not available (API blocked from GitHub Actions servers)"
liq_block = "Not available (API blocked from GitHub Actions servers)"
print("\n📈 Funding/OI unavailable from Actions runners, skipping.")

# ── 3. Fear & Greed ───────────────────────────────────────────────────────
print("\n😨 Fetching Fear & Greed...")
fg_now = {"value": "N/A", "value_classification": "N/A"}
fg_block = "Not available"
try:
    fg_data = json.loads(fetch("https://api.alternative.me/fng/?limit=2"))
    fg_now = fg_data["data"][0]
    fg_prev = fg_data["data"][1]
    fg_block = (
        f"Today: {fg_now['value']} ({fg_now['value_classification']}) | "
        f"Yesterday: {fg_prev['value']} ({fg_prev['value_classification']})"
    )
    print("✅ " + fg_block)
except Exception as e:
    print(f"⚠️  Fear & Greed failed ({e}), continuing without it.")

# ── 4. News ───────────────────────────────────────────────────────────────
print("\n📰 Fetching news...")
news_items = []


def parse_rss(url, source, max_items=5):
    try:
        root = ET.fromstring(fetch(url, retries=2))
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            if title:
                news_items.append(f"[{source}] {title}")
    except Exception as e:
        print(f"⚠️  {source} failed: {e}")


parse_rss("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk")
parse_rss("https://cointelegraph.com/rss", "Cointelegraph")
news_block = "\n".join(news_items[:10]) if news_items else "Not available"
print(f"✅ {len(news_items)} headlines")

# ── 5. Prompt ─────────────────────────────────────────────────────────────
SYSTEM = (
    "You are the lead analyst at ZIMR Capital. Write ONLY in English. "
    "Always use numerals, never spell numbers out. Output plain prose only: "
    "no JSON, no markdown, no headers, no bullet points, no code fences. "
    "Start directly with the first paragraph of the brief."
)

prompt = f"""Write the ZIMR Capital daily market brief for {TODAY}.

Write 4-5 flowing prose paragraphs covering:
1. Overall market: what happened today, total market cap, general mood
2. BTC: exact price, what it did, key levels to watch
3. ETH and notable altcoins: what moved, what didn't, any divergences
4. Macro context: what's driving things (rates, geopolitics, regulation, sentiment)
5. What to watch next: key catalysts, risks, what would change the picture

RULES:
- English only.
- Always use numerals: write 1,250 not "one thousand two hundred fifty",
  and 4.2% not "four point two percent".
- Tone: sharp, confident, trader-to-trader. Like a Bloomberg note, not a chatbot.
- No filler. Every sentence adds information.
- Plain prose only. No bullets, no headers, no formatting of any kind.

CRITICAL DATA RULE:
Every price, percentage, market cap and volume figure you write MUST be copied
verbatim from the DATA block below. Do not round, adjust, extrapolate or invent
any number. If a figure you want to cite is not in the DATA block, describe it
qualitatively instead or leave it out entirely. Numbers mentioned in the news
headlines may be cited but must be attributed to that headline.

═══ DATA (the only numbers you may use) ═══
{market_block}

Best performers 24h among ZIMR tracked majors: {', '.join(gainers)}
Worst performers 24h among ZIMR tracked majors: {', '.join(losers)}

Total crypto market cap: ${total_mcap}T ({mcap_chg:+.2f}% 24h)
24h volume: ${total_vol}B | BTC dominance: {btc_dom}% | ETH dominance: {eth_dom}%

Funding rates: {funding_block}
Open interest: {oi_block}
Recent liquidations: {liq_block}
Fear & Greed: {fg_block}

Latest news headlines:
{news_block}
═══ END DATA ═══

Now write the brief."""

# ── 6. Claude API ─────────────────────────────────────────────────────────
print(f"\n🤖 Generating brief with {MODEL}...")

payload = json.dumps({
    "model": MODEL,
    "max_tokens": 3000,
    "system": SYSTEM,
    "messages": [{"role": "user", "content": prompt}],
}).encode()


def call_claude(payload, api_key, max_retries=3):
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"⚠️  HTTP {e.code} (attempt {attempt}/{max_retries}): {body[:200]}")

            if e.code == 401:
                die("401 Unauthorized. Your CLAUDE_API_KEY secret is invalid "
                    "or expired. Regenerate it in the Anthropic Console.")
            if e.code == 404:
                die(f"404 Not Found for model '{MODEL}'. This model is retired "
                    "or misspelled. Check "
                    "https://docs.claude.com/en/docs/about-claude/models/overview")
            if e.code == 400:
                die(f"400 Bad Request. Your payload is malformed: {body[:300]}")

            if e.code in (429, 500, 502, 503, 529) and attempt < max_retries:
                wait = 45 * attempt
                print(f"⏳ Retrying in {wait}s...")
                time.sleep(wait)
            else:
                die(f"Claude API failed with HTTP {e.code} after "
                    f"{attempt} attempts.")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries:
                wait = 45 * attempt
                print(f"⏳ Network error, retrying in {wait}s...")
                time.sleep(wait)
            else:
                die(f"Could not reach the Claude API: {e}")
    die("Claude API exhausted all retries.")


data = call_claude(payload, api_key)

try:
    text = "".join(
        b["text"] for b in data["content"] if b.get("type") == "text"
    ).strip()
except (KeyError, TypeError) as e:
    die(f"Unexpected API response shape: {e} | {str(data)[:300]}")

# Strip code fences if the model wraps output anyway
text = re.sub(r"^```[a-z]*\s*", "", text)
text = re.sub(r"\s*```$", "", text).strip()

if len(text) < 400:
    die(f"Brief is only {len(text)} chars, that's not a real brief. "
        f"Got: {text[:200]}")

print(f"📝 Preview: {text[:200]}...")

# ── 7. Sanity check: did the model invent a BTC price? ────────────────────
prices_in_text = [
    float(m.replace(",", ""))
    for m in re.findall(r"\$([0-9]{2,3},[0-9]{3}(?:\.[0-9]{2})?)", text)
]
btc_like = [p for p in prices_in_text if abs(p - btc_price) / btc_price < 0.25]

if btc_like and not any(abs(p - btc_price) < 1 for p in btc_like):
    print(f"⚠️  WARNING: brief mentions BTC-range prices {btc_like} but live "
          f"BTC is ${btc_price:,}. The model may be drifting off the data block.")

# ── 8. Write ──────────────────────────────────────────────────────────────
brief = {
    "date": TODAY,
    "generated_at": NOW_ISO,
    "model": MODEL,
    "body": text,
    "btc_price": btc_price,
    "total_mcap_t": total_mcap,
    "btc_dominance": btc_dom,
    "fg_value": fg_now["value"],
    "fg_label": fg_now["value_classification"],
}

os.makedirs("data", exist_ok=True)
with open("data/marketbrief.json", "w") as f:
    json.dump(brief, f, indent=2)

print(f"\n✅ Brief ready for {TODAY}")
print(f"   BTC ${btc_price:,} | F&G {fg_now['value']} | dom {btc_dom}% "
      f"| {len(text)} chars")
