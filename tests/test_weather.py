import httpx
import pytest

from mochi.skills.weather.observer import WeatherObserver, _select_location


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_current", [False, True])
async def test_open_meteo_weather_preserves_numbers_and_rejects_missing_data(
    monkeypatch, missing_current,
):
    import mochi.db as db

    monkeypatch.setattr(db, "get_skill_config", lambda _: {"WEATHER_CITY": "Tokyo"})
    requests = []

    async def get(_client, url, *, params):
        requests.append((url, params))
        payload = (
            {"results": [{"name": "Tokyo", "latitude": 35.7, "longitude": 139.7}]}
            if len(requests) == 1 else
            {} if missing_current else
            {"current": {
                "temperature_2m": 22.5, "apparent_temperature": 24.0,
                "relative_humidity_2m": 60, "weather_code": 61, "wind_speed_10m": 5.4,
            }}
        )
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    observer = WeatherObserver()
    if missing_current:
        with pytest.raises(ValueError, match="missing current weather"):
            await observer.observe()
    else:
        data = await observer.observe()
        assert data["temperature_c"] == 22.5
        assert data["feels_like_c"] == 24
        assert data["wind_kph"] == 5.4
        assert data["condition"] == "slight rain"
        assert data["summary"] == "Tokyo: 22.5°C, Slight rain"
    assert requests[1][1]["latitude"] == 35.7


def test_weather_location_prefers_exact_name_then_population():
    places = [
        {"name": "Tokyo Bay", "population": 1000},
        {"name": "Tokyo", "population": 500},
        {"name": "Tokyo", "population": 100},
    ]
    assert _select_location("Tokyo", places) == places[1]
