from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot.api.routes import budgets, stats, transactions

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
