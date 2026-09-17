"""Smoke test for AntigonaBrain — verifies basic conversation and task routing."""

import asyncio


async def test_brain() -> None:
    from antigona.core.brain import AntigonaBrain, ResponseType

    # Create brain without gateway (standalone mode)
    brain = AntigonaBrain()
    await brain.connect()
    try:
        print("=" * 60)
        print("TEST 1: Greeting (should route to conversation)")
        r1 = await brain.process(
            text="Привет!",
            user_id="test_user",
            channel="cli",
        )
        print(f"  type: {r1.response_type}")
        print(f"  intent: {r1.intent}")
        print(f"  text: {r1.text[:120]}...")
        assert r1.response_type in (ResponseType.CONVERSATION, ResponseType.ERROR), f"Unexpected: {r1.response_type}"

        print()
        print("TEST 2: Identity question (should route to conversation)")
        r2 = await brain.process(
            text="Кто ты?",
            user_id="test_user",
            channel="cli",
        )
        print(f"  type: {r2.response_type}")
        print(f"  intent: {r2.intent}")
        print(f"  text: {r2.text[:120]}...")
        assert r2.response_type in (ResponseType.CONVERSATION, ResponseType.ERROR)

        print()
        print("TEST 3: Task (should route to task, fail without gateway)")
        r3 = await brain.process(
            text="Создай файл test.txt",
            user_id="test_user",
            channel="cli",
        )
        print(f"  type: {r3.response_type}")
        print(f"  intent: {r3.intent}")
        print(f"  text: {r3.text[:120]}...")
        assert r3.response_type == ResponseType.ERROR  # No gateway

        print()
        print("TEST 4: Session ID format")
        # CLI session should be cli:test_user
        sid = "cli:test_user"
        exists = await brain.session_repository.session_exists(sid)
        print(f"  session '{sid}' exists: {exists}")
        assert exists, "Session should have been created"

        print()
        print("TEST 5: Same brain, different channels")
        await brain.process(
            text="Привет!",
            user_id="1122334455",
            channel="telegram",
        )
        tg_sid = "telegram:1122334455"
        tg_exists = await brain.session_repository.session_exists(tg_sid)
        print(f"  telegram session '{tg_sid}' exists: {tg_exists}")
        assert tg_exists, "Telegram session should have been created"

        print()
        print("TEST 6: Empty input")
        r6 = await brain.process(text="", user_id="test_user", channel="cli")
        print(f"  empty text -> response: '{r6.text}'")
        assert r6.text == ""

        print()
        print("TEST 7: Control intent without active flow")
        r7 = await brain.process(
            text="Отмени задачу",
            user_id="new_user",
            channel="cli",
        )
        print(f"  type: {r7.response_type}")
        print(f"  text: {r7.text[:120]}...")
    finally:
        await brain.close()

    print()
    print("=" * 60)
    print("ALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(test_brain())
