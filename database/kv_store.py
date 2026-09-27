# path: database/kv_store.py
"""Key-value store helpers for plugin data persistence"""
import json
from typing import Any, Dict

from sqlalchemy import select, delete

from database.session import get_session
from database.models import KeyValueStore


async def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    """Get a value from the key-value store"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value


async def kv_set(namespace: str, key: str, value: Any) -> None:
    """Set a value in the key-value store"""
    json_value = json.dumps(value)
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = json_value
        else:
            session.add(KeyValueStore(namespace=namespace, key=key, value=json_value))
        await session.commit()


async def kv_delete(namespace: str, key: str) -> bool:
    """Delete a key from the key-value store. Returns True if deleted."""
    async with get_session() as session:
        result = await session.execute(
            delete(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
        )
        await session.commit()
        return result.rowcount > 0


async def kv_get_all(namespace: str) -> Dict[str, Any]:
    """Get all key-value pairs in a namespace"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore).where(KeyValueStore.namespace == namespace)
        )
        rows = result.scalars().all()
        data = {}
        for row in rows:
            try:
                data[row.key] = json.loads(row.value)
            except json.JSONDecodeError:
                data[row.key] = row.value
        return data
