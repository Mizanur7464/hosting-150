# 🤖 Solana Trading Bot

A sophisticated Telegram bot for automated Solana token trading with Jupiter DEX integration.

## ✨ Features

- **Automated Trading**: Buy/sell tokens automatically based on signals
- **Risk Management**: Stop loss, trailing stop, and take profit ladder
- **Smart Re-entry**: Automatic re-entry after profitable exits
- **Real-time Monitoring**: Live price tracking and position management
- **Jupiter DEX Integration**: Seamless token swaps on Solana
- **Telegram Integration**: Channel monitoring and manual controls

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Bot
Edit `bot_config.env` with your details:
- Add your Solana wallet private key
- Set your Telegram channels to monitor
- Adjust trading parameters as needed

### 3. Setup Check
```bash
python setup_bot.py
```

### 4. Run Bot
```bash
python bot.py
```

## ⚙️ Configuration

### Required Environment Variables
- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token
- `RPC_URL`: Helius RPC endpoint
- `WALLET_PRIVATE_KEY`: Your Solana wallet private key
- `TELEGRAM_CHANNELS`: Comma-separated list of channels to monitor

### Trading Parameters
- `TRADE_AMOUNT_USD`: Amount per trade (default: $10)
- `STOP_LOSS_PCT`: Stop loss percentage (default: -30%)
- `TRAIL_FROM_PEAK_PCT`: Trailing stop percentage (default: 15%)
- `TP_LADDER`: Take profit ladder configuration
- `DRY_RUN`: Set to false for live trading

## 🎯 Commands

- `/start` - Show bot status and configuration
- `/buy <TOKEN_MINT>` - Manually buy a token
- `/emergency_sell` - Emergency sell all positions

## ⚠️ Safety Notes

- Always test with `DRY_RUN=true` first
- Start with small amounts
- Monitor your positions regularly
- Keep your private keys secure

## 📞 Support

- DM @mag_eth for support
- Join @gemtools_official
- Use @GemToolsAds_bot for promotions

## 🔧 Technical Details

- Built with Python 3.8+
- Uses Jupiter DEX for swaps
- Helius RPC for Solana connectivity
- Real-time WebSocket monitoring
- Priority fee support for fast execution

---

**⚠️ Disclaimer**: This bot is for educational purposes. Trading cryptocurrencies involves risk. Use at your own discretion.
