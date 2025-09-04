#!/usr/bin/env python3
"""
Bot Setup Script
This script helps you set up the trading bot with proper configuration
"""

import os
import sys

def check_dependencies():
    """Check if all required dependencies are installed"""
    try:
        import telegram
        import httpx
        import websockets
        import solana
        import solders
        print("✅ All dependencies are installed")
        return True
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("Please run: pip install -r requirements.txt")
        return False

def check_config():
    """Check if configuration file exists and has required values"""
    if not os.path.exists("bot_config.env"):
        print("❌ bot_config.env file not found")
        return False
    
    with open("bot_config.env", "r") as f:
        content = f.read()
        
    required_vars = [
        "TELEGRAM_BOT_TOKEN",
        "RPC_URL", 
        "WALLET_PRIVATE_KEY",
        "TELEGRAM_CHANNELS"
    ]
    
    missing_vars = []
    for var in required_vars:
        if f"{var}=" not in content or f"{var}=your_" in content:
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing or incomplete configuration: {', '.join(missing_vars)}")
        print("Please update bot_config.env with your actual values")
        return False
    
    print("✅ Configuration file looks good")
    return True

def main():
    print("🤖 Trading Bot Setup Check")
    print("=" * 40)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check configuration
    if not check_config():
        sys.exit(1)
    
    print("\n✅ Bot is ready to run!")
    print("To start the bot, run: python bot.py")
    print("\n⚠️  Remember:")
    print("- Set DRY_RUN=false for live trading")
    print("- Make sure you have SOL in your wallet")
    print("- Test with small amounts first")

if __name__ == "__main__":
    main()
