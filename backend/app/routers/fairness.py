"""Public, unauthenticated data for independently re-running a draw."""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import get_conn
from app.routers import ERRORS
from app.schemas import FairnessEntrants, FairnessResponse, FairnessResults
from app.services import fairness

router = APIRouter(prefix="/events/{event_id}/fairness", tags=["fairness"], responses=ERRORS)


@router.get("", response_model=FairnessResponse)
async def get_fairness(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> FairnessResponse:
    """Commitment, beacon, and (after the draw) seed, hashes and result counts."""
    return await fairness.get_fairness(conn, event_id)


@router.get("/entrants", response_model=FairnessEntrants)
async def get_entrants(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> FairnessEntrants:
    """Pseudonymous entrant list in canonical order. 409 until the window closes."""
    return await fairness.get_entrants(conn, event_id)


@router.get("/results", response_model=FairnessResults)
async def get_results(event_id: UUID, conn: AsyncConnection = Depends(get_conn)) -> FairnessResults:
    """Winners and waitlist (public ids) in draw order. 409 until the draw has run."""
    return await fairness.get_results(conn, event_id)
