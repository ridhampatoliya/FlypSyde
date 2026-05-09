import anthropic
import base64
import json
import os
from config import TAKE_PROFIT_PCT_DEFAULTS, STOP_LOSS_PCT, TAKE_PROFIT_MAX_PCT

SYSTEM_PROMPT = """You are an AI trading assistant analyzing content from the Reinvest app to find short-term stock trading opportunities.

Notation system:
- ✅ = Verified data (factual)
- 😇 = Analyst opinion (personal view — weigh heavily)
- ⚠️ = Critical information
- ➡️ = Sub notes
- GREEN FLAG = positive signal for a stock
- RED FLAG = negative signal for a stock
- KPEG = custom PEG ratio variant (< 1.0 = good value, < 0.5 = very attractive)

CONVICTION SCORING:
- HIGH: 3+ green flags AND strong bullish analyst opinion AND KPEG < 1.0 (if shown) AND no major red flags
- MEDIUM: 2 green flags OR mixed signals (positive thesis but real concerns like negative margins, rising expenses)
- LOW: 1 green flag OR analyst is cautiously optimistic but has significant reservations
- SKIP: Bearish context, mentioned only as something to sell/avoid, or red flags dominate

TAKE-PROFIT TARGETS:
- If a specific price target is stated, calculate % gain from the current price shown in the app
- Otherwise: High=20%, Medium=12%, Low=8%
- Hard cap at 25% (conservative — we are not trying to hit home runs)
- If the data says "next few months" with a target, still use the % but note the longer timeframe

STOP-LOSS:
- High conviction: 10% (give the thesis room to play out)
- Medium conviction: 7%
- Low conviction: 5%

IMPORTANT RULES:
- Only include stocks where the analyst is clearly bullish or strongly considering buying
- Exclude stocks mentioned only negatively, as macro examples, or as things to avoid/sell
- If the overall market sentiment is bearish, be more conservative — drop low conviction picks
- Be conservative by default: when in doubt, score one level lower or skip

Return ONLY valid JSON, no other text, no markdown fences:
{
  "market_sentiment": "bullish|neutral|bearish",
  "market_context": "1-2 sentence summary of overall market conditions from the data",
  "trades": [
    {
      "ticker": "SYMBOL",
      "company_name": "Full Company Name",
      "conviction": "high|medium|low",
      "reasoning": "concise explanation referencing specific signals",
      "green_flags": 0,
      "red_flags": 0,
      "kpeg": null,
      "kevin_sentiment": "brief phrase describing analyst stance",
      "take_profit_pct": 15.0,
      "stop_loss_pct": 7.0,
      "price_target_mentioned": null,
      "notes": "important caveats or risks flagged"
    }
  ],
  "skipped": [
    {
      "ticker": "SYMBOL",
      "reason": "why excluded"
    }
  ]
}"""


def analyze_morning_data(text: str, images: list, history_context: str = "") -> dict:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    content = []

    for img in images:
        img.seek(0)
        img_bytes = img.read()
        encoded = base64.standard_b64encode(img_bytes).decode("utf-8")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.type,
                "data": encoded,
            },
        })

    if text and text.strip():
        content.append({
            "type": "text",
            "text": f"Morning Reinvest data:\n\n{text.strip()}",
        })

    if not content:
        return {
            "market_sentiment": "neutral",
            "market_context": "No data provided.",
            "trades": [],
            "skipped": [],
        }

    if history_context:
        content.append({
            "type": "text",
            "text": history_context,
        })

    content.append({
        "type": "text",
        "text": "Analyze the above Reinvest content and return the JSON trading analysis.",
    })

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    result = json.loads(raw)

    # Apply caps and fill missing values
    for trade in result.get("trades", []):
        conviction = trade.get("conviction", "medium")

        if not trade.get("take_profit_pct"):
            trade["take_profit_pct"] = TAKE_PROFIT_PCT_DEFAULTS.get(conviction, 12.0)
        else:
            trade["take_profit_pct"] = min(float(trade["take_profit_pct"]), TAKE_PROFIT_MAX_PCT)

        if not trade.get("stop_loss_pct"):
            trade["stop_loss_pct"] = STOP_LOSS_PCT.get(conviction, 7.0)

    return result
