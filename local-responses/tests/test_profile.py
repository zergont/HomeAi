from __future__ import annotations

import json
import pytest
from httpx import AsyncClient, ASGITransport

from apps.api.main import app


@pytest.mark.asyncio
async def test_profile_default_and_put_roundtrip():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET default
        r = await client.get('/profile')
        assert r.status_code == 200
        data = r.json()
        assert 'core_tokens' in data and 'core_cap' in data
        assert isinstance(data['core_tokens'], int)

        # PUT with valid data
        payload = {
            "display_name": "Alex",
            "preferred_language": "ru",
            "tone": "concise",
            "timezone": "+03:00",
            "region_coarse": "RU",
            "work_hours": "10-19",
            "ui_format_prefs": {"bullets": True},
            "goals_mood": "be productive",
            "decisions_tasks": "ship features",
            "brevity": "short",
            "format_defaults": {"tables": "md"},
            "interests_topics": ["ai","python"],
            "workflow_tools": ["git","make"],
            "os": "Windows",
            "runtime": "Python 3.13",
            "hardware_hint": "RTX",
            "source": "user",
            "confidence": 80,
        }
        r2 = await client.put('/profile', json=payload)
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2['display_name'] == 'Alex'
        assert isinstance(data2['core_tokens'], int)
        assert isinstance(data2['core_cap'], int)

        # GET after PUT
        r3 = await client.get('/profile')
        assert r3.status_code == 200
        data3 = r3.json()
        assert data3['display_name'] == 'Alex'
        assert data3['core_tokens'] == data2['core_tokens']
        assert data3['core_cap'] == data2['core_cap']
