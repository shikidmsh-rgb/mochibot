"""Smoke test for Oura integration — mock data, no real API calls."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import asyncio
import time
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

# ── Mock data that looks like real Oura API responses ──
TODAY = datetime.now(timezone(timedelta(hours=0))).strftime("%Y-%m-%d")

MOCK_SLEEP = {
    "data": [{
        "day": TODAY,
        "total_sleep_duration": 27360,   # 7h 36m
        "deep_sleep_duration": 5400,     # 1h 30m
        "rem_sleep_duration": 7200,      # 2h
        "light_sleep_duration": 14760,   # 4h 6m
        "efficiency": 92,
        "average_heart_rate": 58,
        "average_hrv": 42,
        "lowest_heart_rate": 51,
        "bedtime_start": f"{TODAY}T23:15:00+00:00",
        "bedtime_end": f"{TODAY}T07:22:00+00:00",
    }]
}

MOCK_DAILY_SLEEP = {
    "data": [{"day": TODAY, "score": 82}]
}

MOCK_ACTIVITY = {
    "data": [{"day": TODAY, "score": 75, "steps": 8432, "active_calories": 320, "total_calories": 2150}]
}

MOCK_READINESS = {
    "data": [{"day": TODAY, "score": 88, "temperature_deviation": 0.3}]
}

MOCK_STRESS = {
    "data": [{"day": TODAY, "stress_high": 1800, "recovery_high": 3600, "day_summary": "normal"}]
}


def mock_api_get(endpoint, params=None):
    """Simulate Oura API responses."""
    if endpoint == "sleep":
        return MOCK_SLEEP
    elif endpoint == "daily_sleep":
        return MOCK_DAILY_SLEEP
    elif endpoint == "daily_activity":
        return MOCK_ACTIVITY
    elif endpoint == "daily_readiness":
        return MOCK_READINESS
    elif endpoint == "daily_stress":
        return MOCK_STRESS
    return None


def run():
    passed = 0
    failed = 0

    print("=" * 60)
    print("🧪 Oura Integration Test (Mock Data)")
    print(f"   Today = {TODAY}")
    print("=" * 60)
    print()

    # ── Test 1: oura_client functions ──
    print("── Test 1: oura_client.get_daily_summary() ──")
    try:
        with patch("mochi.oura_client._api_get", side_effect=mock_api_get), \
             patch("mochi.oura_client.is_configured", return_value=True), \
             patch("mochi.oura_client._cache", {}):

            from mochi.oura_client import get_daily_summary

            summary = get_daily_summary()
            assert summary is not None, "summary should not be None"
            raw = summary["raw"]

            print(f"  data_date: {summary['data_date']}")
            print(f"  sleep_score: {raw.get('sleep_score')}")
            print(f"  sleep.total: {raw['sleep']['total']}s = {raw['sleep']['total']/3600:.1f}h")
            print(f"  sleep.deep: {raw['sleep']['deep']}s = {raw['sleep']['deep']/3600:.1f}h")
            print(f"  sleep.efficiency: {raw['sleep']['efficiency']}%")
            print(f"  sleep.avg_hrv: {raw['sleep']['avg_hrv']}ms")
            print(f"  activity.steps: {raw['activity']['steps']}")
            print(f"  activity.score: {raw['activity']['score']}")
            print(f"  readiness.score: {raw['readiness']['score']}")
            print(f"  readiness.temp_dev: {raw['readiness']['temperature_deviation']}°C")
            print(f"  stress.day_summary: {raw['stress']['day_summary']}")
            print("  ✅ PASS")
            passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        failed += 1

    print()

    # ── Test 2: Observer ──
    print("── Test 2: OuraObserver.observe() ──")
    try:
        with patch("mochi.oura_client._api_get", side_effect=mock_api_get), \
             patch("mochi.oura_client.is_configured", return_value=True), \
             patch("mochi.oura_client._cache", {}):

            from mochi.observers.oura.observer import OuraObserver
            obs = OuraObserver()
            result = asyncio.run(obs.observe())

            assert result.get("available") is True, "should be available"
            print(f"  available: {result['available']}")
            print(f"  data_date: {result['data_date']}")

            if "sleep" in result:
                print(f"  sleep.total_hours: {result['sleep']['total_hours']}")
                print(f"  sleep.deep_hours: {result['sleep']['deep_hours']}")
                print(f"  sleep.avg_hrv: {result['sleep']['avg_hrv']}ms")
            else:
                print("  (sleep not synced for today — expected if TZ mismatch)")

            if "sleep_score" in result:
                print(f"  sleep_score: {result['sleep_score']}")
            if "activity" in result:
                print(f"  activity.steps: {result['activity']['steps']}")
            if "readiness" in result:
                print(f"  readiness.score: {result['readiness']['score']}")
            if "stress" in result:
                print(f"  stress.day_summary: {result['stress']['day_summary']}")
            if "sleep_not_synced" in result:
                print(f"  sleep_not_synced: {result['sleep_not_synced']}")

            print(f"  baselines: {result.get('baselines', {})}")
            print("  ✅ PASS")
            passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        failed += 1

    print()

    # ── Test 3: Skill — all categories ──
    print("── Test 3: OuraSkill (all categories) ──")
    try:
        with patch("mochi.oura_client._api_get", side_effect=mock_api_get), \
             patch("mochi.oura_client.is_configured", return_value=True), \
             patch("mochi.oura_client._cache", {}):

            from mochi.skills.oura.handler import OuraSkill
            from mochi.skills.base import SkillContext

            skill = OuraSkill()

            # category=all
            ctx = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "all"})
            sr = asyncio.run(skill.execute(ctx))
            data = json.loads(sr.output)
            assert data["available"] is True
            print(f"  all: keys={list(data['data'].keys())}")

            # category=sleep
            ctx2 = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "sleep"})
            sr2 = asyncio.run(skill.execute(ctx2))
            data2 = json.loads(sr2.output)
            print(f"  sleep: score={data2['data'].get('score')}, total_sec={data2['data'].get('total_sleep_sec')}")

            # category=activity
            ctx3 = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "activity"})
            sr3 = asyncio.run(skill.execute(ctx3))
            data3 = json.loads(sr3.output)
            print(f"  activity: steps={data3['data'].get('steps')}, score={data3['data'].get('score')}")

            # category=readiness
            ctx4 = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "readiness"})
            sr4 = asyncio.run(skill.execute(ctx4))
            data4 = json.loads(sr4.output)
            print(f"  readiness: score={data4['data'].get('score')}")

            # category=stress
            ctx5 = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "stress"})
            sr5 = asyncio.run(skill.execute(ctx5))
            data5 = json.loads(sr5.output)
            print(f"  stress: day_summary={data5['data'].get('day_summary')}")

            # bad category
            ctx6 = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={"category": "bogus"})
            sr6 = asyncio.run(skill.execute(ctx6))
            data6 = json.loads(sr6.output)
            assert "unknown category" in data6["error"]
            print(f"  bogus: error={data6['error']}")

            # wrong tool name
            ctx7 = SkillContext(trigger="tool_call", tool_name="wrong_tool", args={})
            sr7 = asyncio.run(skill.execute(ctx7))
            assert sr7.success is False
            print(f"  wrong_tool: success={sr7.success}")

            print("  ✅ PASS")
            passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        failed += 1

    print()

    # ── Test 4: Not configured ──
    print("── Test 4: Not configured (graceful) ──")
    try:
        with patch("mochi.oura_client.is_configured", return_value=False):
            from mochi.observers.oura.observer import OuraObserver
            from mochi.skills.oura.handler import OuraSkill
            from mochi.skills.base import SkillContext

            obs = OuraObserver()
            result = asyncio.run(obs.observe())
            assert result == {}, f"Expected empty dict, got {result}"
            print("  Observer returns: {} ✅")

            skill = OuraSkill()
            ctx = SkillContext(trigger="tool_call", tool_name="get_oura_data", args={})
            sr = asyncio.run(skill.execute(ctx))
            data = json.loads(sr.output)
            assert data["error"] == "oura_not_configured"
            print(f"  Skill returns: error=oura_not_configured ✅")

            print("  ✅ PASS")
            passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        failed += 1

    print()

    # ── Test 5: SKILL.md parsing ──
    print("── Test 5: SKILL.md tool definition ──")
    try:
        from mochi.skills.oura.handler import OuraSkill
        skill = OuraSkill()
        tools = skill.get_tools()
        assert len(tools) > 0, "Should have at least one tool"
        fn = tools[0]["function"]
        print(f"  tool name: {fn['name']}")
        print(f"  params: {list(fn['parameters']['properties'].keys())}")
        print(f"  required: {fn['parameters'].get('required', [])}")
        assert fn["name"] == "get_oura_data"
        assert "category" in fn["parameters"]["properties"]
        assert "date" in fn["parameters"]["properties"]
        print("  ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        failed += 1

    print()

    # ── Test 6: OBSERVATION.md parsing ──
    print("── Test 6: OBSERVATION.md metadata ──")
    try:
        from mochi.observers.oura.observer import OuraObserver
        obs = OuraObserver()
        print(f"  name: {obs.meta.name}")
        print(f"  interval: {obs.meta.interval}m")
        print(f"  enabled: {obs.meta.enabled}")
        print(f"  requires_config: {obs.meta.requires_config}")
        assert obs.meta.name == "oura"
        assert obs.meta.interval == 30
        assert obs.meta.enabled is True
        print("  ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"  ❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        failed += 1

    print()
    print("=" * 60)
    if failed == 0:
        print(f"✅ ALL {passed} TESTS PASSED")
    else:
        print(f"❌ {passed} passed, {failed} FAILED")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
