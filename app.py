import os
import streamlit as st
from dotenv import load_dotenv
from agent import analyze_morning_data
from broker import Broker
import config

load_dotenv()

st.set_page_config(page_title="AutoInvest", page_icon="📈", layout="wide")


@st.cache_resource
def get_broker():
    return Broker(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))


# ── Sidebar: live account info ─────────────────────────────────────────────────
with st.sidebar:
    st.title("Account")
    try:
        broker = get_broker()
        acc = broker.get_account()
        market_open = broker.is_market_open()
        st.metric("Cash", f"${acc['cash']:,.2f}")
        st.metric("Portfolio Value", f"${acc['portfolio_value']:,.2f}")
        st.write("Market:", "🟢 Open" if market_open else "🔴 Closed")
        if not market_open:
            st.caption("Orders placed now will fill at next market open.")
    except Exception as e:
        st.warning(f"Alpaca: {e}")

    st.divider()
    st.caption(f"Simulated capital: ${config.TOTAL_CAPITAL:,.0f}")
    st.caption(f"Cash reserve: {config.CASH_RESERVE_PCT * 100:.0f}% (${config.TOTAL_CAPITAL * config.CASH_RESERVE_PCT:,.0f})")
    st.caption(f"Max deploy: ${config.MAX_TOTAL_DEPLOYED:,.0f} across {config.MAX_POSITIONS} positions")
    st.divider()
    st.caption("Conviction → position size")
    st.caption(f"🔥 High → ${config.POSITION_SIZES['high']:.0f}  |  TP +{config.TAKE_PROFIT_PCT_DEFAULTS['high']}%  |  SL -{config.STOP_LOSS_PCT['high']}%")
    st.caption(f"⚡ Medium → ${config.POSITION_SIZES['medium']:.0f}  |  TP +{config.TAKE_PROFIT_PCT_DEFAULTS['medium']}%  |  SL -{config.STOP_LOSS_PCT['medium']}%")
    st.caption(f"💧 Low → ${config.POSITION_SIZES['low']:.0f}  |  TP +{config.TAKE_PROFIT_PCT_DEFAULTS['low']}%  |  SL -{config.STOP_LOSS_PCT['low']}%")


# ── Main: input ────────────────────────────────────────────────────────────────
st.title("📈 AutoInvest")
st.caption("Paste or screenshot your morning Reinvest data → Claude scores conviction → Alpaca executes.")

st.subheader("Morning Input")
col_left, col_right = st.columns(2)

with col_left:
    text_input = st.text_area(
        "Paste from Reinvest (Alpha, Data, or Stocks tabs)",
        height=360,
        placeholder=(
            "Paste any text from the Reinvest app here.\n"
            "You can combine multiple tabs in one block.\n\n"
            "Leave blank if using screenshots only."
        ),
    )

with col_right:
    uploaded_images = st.file_uploader(
        "Upload screenshots (optional)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="Screenshots from any Reinvest tab — Alpha, Data, Stocks.",
    )
    if uploaded_images:
        thumb_cols = st.columns(min(len(uploaded_images), 2))
        for i, img in enumerate(uploaded_images):
            thumb_cols[i % 2].image(img, use_column_width=True)

has_input = bool(text_input and text_input.strip()) or bool(uploaded_images)

if st.button("Analyze", type="primary", disabled=not has_input):
    with st.spinner("Claude is reading your morning data..."):
        try:
            result = analyze_morning_data(text_input or "", uploaded_images or [])
            st.session_state.analysis = result
            st.session_state.executed = False
        except Exception as e:
            st.error(f"Analysis error: {e}")

# ── Results ────────────────────────────────────────────────────────────────────
if st.session_state.get("analysis"):
    analysis = st.session_state.analysis
    trades = analysis.get("trades", [])
    skipped = analysis.get("skipped", [])

    st.divider()
    st.subheader("Analysis")

    sentiment = analysis.get("market_sentiment", "neutral")
    icon = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(sentiment, "🟡")
    st.markdown(f"**Market Sentiment:** {icon} {sentiment.upper()}")
    if analysis.get("market_context"):
        st.caption(analysis["market_context"])

    if not trades:
        st.info("No actionable trades found. Market may be bearish or no stocks were clearly bullish.")
    else:
        total_deploy = sum(config.POSITION_SIZES.get(t["conviction"], 0) for t in trades)
        st.subheader(f"Proposed Trades — ${total_deploy:.0f} deployed")

        conviction_icon = {"high": "🔥", "medium": "⚡", "low": "💧"}

        for trade in trades:
            conviction = trade["conviction"]
            notional = config.POSITION_SIZES.get(conviction, 100)
            ci = conviction_icon.get(conviction, "")
            label = f"{ci} **{trade['ticker']}**"
            if trade.get("company_name"):
                label += f" — {trade['company_name']}"
            label += f" — {conviction.upper()} — ${notional:.0f}"

            with st.expander(label, expanded=True):
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Position", f"${notional:.0f}")
                m2.metric("Take Profit", f"+{trade['take_profit_pct']:.1f}%")
                m3.metric("Stop Loss", f"-{trade['stop_loss_pct']:.1f}%")
                m4.metric("KPEG", str(trade["kpeg"]) if trade.get("kpeg") else "—")

                flags = f"Green flags: {trade.get('green_flags', '?')}  |  Red flags: {trade.get('red_flags', '?')}"
                if trade.get("price_target_mentioned"):
                    flags += f"  |  Analyst target: ${trade['price_target_mentioned']}"
                st.caption(flags)

                st.write(f"**Reasoning:** {trade['reasoning']}")
                if trade.get("notes"):
                    st.warning(f"Caveat: {trade['notes']}")

        if skipped:
            with st.expander(f"Skipped ({len(skipped)})"):
                for s in skipped:
                    st.write(f"**{s.get('ticker', '?')}** — {s.get('reason', '')}")

        st.divider()

        if st.session_state.get("executed"):
            st.success("All orders submitted to Alpaca paper trading.")
        else:
            st.warning(
                f"Review the trades above. Clicking Execute will place real bracket orders "
                f"on your Alpaca paper account. Total deployment: **${total_deploy:.0f}**."
            )
            if st.button("Execute All Trades on Alpaca Paper", type="primary"):
                broker = get_broker()
                progress = st.progress(0)

                results = []
                for i, trade in enumerate(trades):
                    notional = config.POSITION_SIZES.get(trade["conviction"], 100)
                    order, error = broker.place_bracket_order(
                        trade["ticker"],
                        notional,
                        trade["take_profit_pct"],
                        trade["stop_loss_pct"],
                    )
                    results.append((trade["ticker"], order, error))
                    progress.progress((i + 1) / len(trades))

                st.session_state.executed = True

                for ticker, order, error in results:
                    if error:
                        st.error(f"**{ticker}**: {error}")
                    else:
                        if order.get("fractional"):
                            st.warning(
                                f"**{ticker}**: {order['shares']:.4f} shares @ ~\${order['entry_price']:.2f} | "
                                f"Order ID: {order['id']} | ⚠️ Fractional — set TP \${order['take_profit_price']:.2f} / SL \${order['stop_loss_price']:.2f} manually in Alpaca"
                            )
                        else:
                            st.success(
                                f"**{ticker}**: {order['shares']} shares @ ~\${order['entry_price']:.2f} | "
                                f"TP \${order['take_profit_price']:.2f} | SL \${order['stop_loss_price']:.2f} | "
                                f"Order ID: {order['id']}"
                            )
