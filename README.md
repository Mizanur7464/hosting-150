# 🌊 Leviathan86Bot - Advanced Solana Trading Bot

## 🚀 Features

### ⚓ **Wallet Management**
- Secure private key storage
- Multiple wallet support
- Real-time balance tracking

### ⚔️ **Trading System**
- **Percentage Trading**: 5% of wallet (grows over time)
- **Fixed Amount**: $10 - $10,000 (constant amount)
- **Choose Mode**: Either percentage OR fixed amount
- **Jupiter Integration**: Solana DEX integration
- **Real-time Price**: Jupiter API price feeds

### 🛡️ **Risk Management**
- **Stop Loss**: -30% (configurable)
- **Trailing Stop**: 15% from peak
- **Advanced Take Profit Ladder**: 
  - 30% at 2x
  - 20% at 5x
  - 10% at 10x
  - 15% at 15x
  - 15% at 20x
  - Rest: Trailing stop
- **Re-entry System**: Disabled (buyer preference)

### 🌊 **Leviathan Mode**
- **Live Trading**: ON/OFF toggle
- **Dry Run Mode**: Testing without real trades
- **Status Monitoring**: Real-time bot status

### 🪝 **Channel Monitoring**
- **@gem_tools_calls**: Auto signal detection
- **Token Parsing**: Regex-based mint extraction
- **Auto Trading**: Instant buy on signal

## 📋 Installation

1. **Clone Repository**
```bash
git clone <repository-url>
cd hosting-150
```

2. **Install Dependencies**
```bash
pip install -r requirements.txt
```

3. **Configure Environment**
```bash
# Copy environment template
copy env_example.txt .env

# Edit .env file with your values
# - TELEGRAM_BOT_TOKEN
# - RPC_URL
# - WALLET_PRIVATE_KEY
# - All other settings
```

4. **Run Bot**
```bash
python bot.py
```

## ⚙️ Configuration

### Environment Variables (.env)
```env
# Telegram Bot
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHANNELS=@gem_tools_calls

# Solana RPC
RPC_URL=https://mainnet.helius-rpc.com/?api-key=your_key
WALLET_PRIVATE_KEY=your_private_key

# Trading Settings
TRADE_AMOUNT_USD=10.0
TRADE_PERCENTAGE=5.0
USE_PERCENTAGE_TRADING=True
STOP_LOSS_PCT=-30.0
TRAIL_FROM_PEAK_PCT=15.0
TP_LADDER=2x:30,5x:20,10x:10,15x:15,20x:15,rest:trail15

# Re-entry Settings
REENTRY_ENABLED=True
REENTRY_CONFIRM_PCT=7.0
MAX_REENTRIES_PER_TOKEN=1

# System Settings
DRY_RUN=False
PRICE_POLL_SECONDS=0.5
PRIORITY_FEE_MICROLAMPORTS=20000
MIN_LIQ_SOL=10.0

# Solana Constants
SOL_MINT=So11111111111111111111111111111111111111112
LAMPORTS_PER_SOL=1000000000
```

## 🎯 **New Ladder Strategy**

### **Profit Distribution:**
- **2x**: 30% of position sold
- **5x**: 20% of position sold
- **10x**: 10% of position sold
- **15x**: 15% of position sold
- **20x**: 15% of position sold
- **Rest**: Trailing stop (15% from peak)

### **Benefits:**
- **Risk Reduction**: Early profit taking
- **Maximize Gains**: Hold for higher multiples
- **Flexible Strategy**: Adapts to market conditions
- **Trailing Stop**: Protects remaining position

## 🔒 Security

- **Environment Variables**: All sensitive data in .env
- **No Hardcoded Values**: Zero hardcoded secrets
- **Git Protection**: .env file not committed
- **Validation**: All required variables checked

## 📊 Performance

- **Buy Speed**: 1-2 seconds
- **Price Polling**: Every 0.5 seconds
- **Channel Monitoring**: Real-time
- **Transaction Speed**: Sub-500ms reactivity

## 🛠️ Commands

- `/start` - Main menu
- `/buy <TOKEN_MINT>` - Manual buy
- `/emergency_sell` - Sell all positions

## 📱 Telegram Interface

### **Main Menu:**
- ⚓ Wallet Dock
- ⚔️ Trade Settings
- 🌊 Leviathan Mode
- 🪝 Sniping Grounds
- 📜 Navigation & Logs
- ⚙️ Leviathan Forge

## 🚨 Important Notes

1. **Bot Admin**: Bot must be admin in monitored channels
2. **Private Keys**: Only use in private chat
3. **Testing**: Use DRY_RUN=true for testing
4. **Backup**: Always backup your .env file
5. **Updates**: Check for updates regularly

## 📞 Support

For support and updates, contact the development team.

---

**⚠️ Disclaimer**: Trading cryptocurrencies involves risk. Use at your own discretion.