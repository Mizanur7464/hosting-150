#!/usr/bin/env python3
"""
Bot Test Script
Test the bot configuration and basic functionality
"""

import asyncio
import os
from dotenv import load_dotenv

# Load configuration
load_dotenv("bot_config.env")

async def test_config():
    """Test bot configuration"""
    print("🧪 Testing Bot Configuration")
    print("=" * 40)
    
    # Check required environment variables
    required_vars = {
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "RPC_URL": os.getenv("RPC_URL"),
        "WALLET_PRIVATE_KEY": os.getenv("WALLET_PRIVATE_KEY"),
        "TELEGRAM_CHANNELS": os.getenv("TELEGRAM_CHANNELS")
    }
    
    for var, value in required_vars.items():
        if value and value != "your_wallet_private_key_here":
            print(f"✅ {var}: {'*' * 10}...{value[-4:]}")
        else:
            print(f"❌ {var}: Not configured")
    
    print(f"\n📊 Trading Configuration:")
    print(f"   Trade Amount: ${os.getenv('TRADE_AMOUNT_USD', '10')}")
    print(f"   Stop Loss: {os.getenv('STOP_LOSS_PCT', '-30')}%")
    print(f"   Trail Stop: {os.getenv('TRAIL_FROM_PEAK_PCT', '15')}%")
    print(f"   Dry Run: {os.getenv('DRY_RUN', 'true')}")
    
    print(f"\n📡 Channels to Monitor:")
    channels = os.getenv("TELEGRAM_CHANNELS", "").split(",")
    for channel in channels:
        if channel.strip():
            print(f"   - {channel.strip()}")

async def test_jupiter_connection():
    """Test Jupiter API connection"""
    print("\n🌐 Testing Jupiter API Connection")
    print("=" * 40)
    
    try:
        import httpx
        
        # Test Jupiter price API
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://price.jup.ag/v6/price", 
                                     params={"ids": "So11111111111111111111111111111111111111112", 
                                           "vsToken": "So11111111111111111111111111111111111111112"})
            
            if response.status_code == 200:
                print("✅ Jupiter Price API: Connected")
            else:
                print(f"❌ Jupiter Price API: Error {response.status_code}")
                
    except Exception as e:
        print(f"❌ Jupiter API Error: {e}")

async def test_solana_connection():
    """Test Solana RPC connection"""
    print("\n⛓️ Testing Solana RPC Connection")
    print("=" * 40)
    
    try:
        from solana.rpc.async_api import AsyncClient
        
        rpc_url = os.getenv("RPC_URL")
        if not rpc_url:
            print("❌ RPC_URL not configured")
            return
            
        client = AsyncClient(rpc_url)
        
        # Test connection
        version = await client.get_version()
        if version:
            print("✅ Solana RPC: Connected")
            print(f"   Version: {version.value.get('solana-core', 'Unknown')}")
        else:
            print("❌ Solana RPC: Connection failed")
            
        await client.close()
        
    except Exception as e:
        print(f"❌ Solana RPC Error: {e}")

async def main():
    print("🤖 Bot Test Suite")
    print("=" * 50)
    
    await test_config()
    await test_jupiter_connection()
    await test_solana_connection()
    
    print("\n" + "=" * 50)
    print("✅ Test completed!")
    print("\nNext steps:")
    print("1. Update WALLET_PRIVATE_KEY in bot_config.env")
    print("2. Run: python bot.py")
    print("3. Test with /start command in Telegram")

if __name__ == "__main__":
    asyncio.run(main())
