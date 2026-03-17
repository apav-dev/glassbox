from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from glassbox.clients.alpaca import AlpacaClient
from glassbox.db.models import get_db


DbSession = Annotated[Session, Depends(get_db)]


@lru_cache(maxsize=1)
def get_alpaca_client() -> AlpacaClient:
    return AlpacaClient()

