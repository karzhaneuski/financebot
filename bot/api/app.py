from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bot.api.routes import budgets, stats, transactions
from bot.db.engine import AsyncSessionLocal

app = FastAPI(title="FinanceBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stats.router, prefix="/api/stats")
app.include_router(budgets.router, prefix="/api")
app.include_router(transactions.router, prefix="/api/transactions")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Liveness + DB reachability for the container healthcheck. No auth."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}
