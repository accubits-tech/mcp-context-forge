#!/usr/bin/env python3
"""Diagnostic script to verify OAuth state storage/retrieval."""

import asyncio
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    """Run OAuth state diagnostics."""
    from mcpgateway.config import get_settings

    settings = get_settings()

    print("=" * 60)
    print("OAuth State Storage Diagnostics")
    print("=" * 60)
    print(f"\nCache Type: {settings.cache_type}")
    print(f"Redis URL: {settings.redis_url}")
    print(f"Database URL: {settings.database_url[:50]}..." if settings.database_url else "Database URL: Not set")

    if settings.cache_type == "redis":
        print("\n--- Testing Redis Connection ---")
        try:
            import aioredis

            redis = await aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            pong = await redis.ping()
            print(f"✅ Redis PING: {pong}")

            # Test state storage
            test_key = "oauth:state:test:diagnostic_test_state"
            test_value = '{"state": "test", "gateway_id": "test", "code_verifier": "test", "expires_at": "2099-01-01T00:00:00+00:00", "used": false}'

            await redis.setex(test_key, 300, test_value)
            print(f"✅ Test state stored in Redis")

            retrieved = await redis.get(test_key)
            if retrieved == test_value:
                print(f"✅ Test state retrieved successfully")
            else:
                print(f"❌ Retrieved value mismatch!")
                print(f"   Expected: {test_value}")
                print(f"   Got: {retrieved}")

            # Check for any existing OAuth states
            keys = await redis.keys("oauth:state:*")
            print(f"\n📊 Current OAuth states in Redis: {len(keys)}")
            for key in keys[:5]:  # Show first 5
                ttl = await redis.ttl(key)
                print(f"   - {key[:60]}... (TTL: {ttl}s)")
            if len(keys) > 5:
                print(f"   ... and {len(keys) - 5} more")

            # Cleanup test key
            await redis.delete(test_key)
            print("\n✅ Test key cleaned up")

            await redis.close()
        except Exception as e:
            print(f"❌ Redis Error: {e}")

    elif settings.cache_type == "database":
        print("\n--- Testing Database Connection ---")
        try:
            from mcpgateway.db import get_db, OAuthState

            db_gen = get_db()
            db = next(db_gen)

            # Count current states
            count = db.query(OAuthState).count()
            print(f"✅ Database connection successful")
            print(f"📊 Current OAuth states in database: {count}")

            # Show recent states
            states = db.query(OAuthState).order_by(OAuthState.created_at.desc()).limit(5).all()
            for state in states:
                print(f"   - Gateway: {state.gateway_id}, Expires: {state.expires_at}, Used: {state.used}")

            db_gen.close()
        except Exception as e:
            print(f"❌ Database Error: {e}")

    else:
        print(f"\n⚠️  Using in-memory storage (cache_type={settings.cache_type})")
        print("   States will be lost on server restart and not shared between workers!")

    print("\n" + "=" * 60)
    print("Diagnostics complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
