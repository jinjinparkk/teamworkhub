from fastapi import APIRouter

router = APIRouter()


@router.get("/health", summary="Liveness probe")
def health() -> dict:
    """Returns 200 immediately.  No auth required.
    Cloud Run health-check and Cloud Scheduler OIDC pre-flight both use this."""
    return {"status": "ok", "service": "teamworkhub"}
