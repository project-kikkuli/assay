"""Public fixtures for API/schema/browser boundary probes; run inside subject env."""
import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session
from app.core.db import engine
from app.core.security import create_access_token
from app.crud import DUMMY_HASH
from app.models import User, Item

with Session(engine) as session:
    user = User(email="boundary@example.com", hashed_password=DUMMY_HASH)
    session.add(user)
    session.flush()
    items = [Item(owner_id=user.id, title=f"fixture-{i:03}",
                  created_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=i)) for i in range(150)]
    items[-1].title = '<svg onload="globalThis.__assayInjected=true"></svg>'
    session.add_all(items)
    session.commit()
    print(json.dumps({"owner": str(user.id), "item": str(items[0].id), "seeded_count": len(items),
                      "token": create_access_token(user.id, timedelta(minutes=10))}))
