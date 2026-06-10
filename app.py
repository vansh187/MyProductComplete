from fastapi import FastAPI
from pydantic import BaseModel
from api.signup import router as signup_router
from api.login import router as login_router
from api.orders import router as orders_router
from api.trade import router as trade_history
from api.portfolio import router as user_portfolio
app=FastAPI()

app.include_router(orders_router)
app.include_router(signup_router)
app.include_router(login_router)
app.include_router(trade_history)
app.include_router(user_portfolio)

@app.get("/")
def read_root():
    return {"Message":"Finnaly I am able to run my first API"}





