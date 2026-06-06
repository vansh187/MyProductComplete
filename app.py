from fastapi import FastAPI
from pydantic import BaseModel
from api.signup import router as signup_router
from api.login import router as login_router
app=FastAPI()

app.include_router(signup_router)
app.include_router(login_router)
@app.get("/")
def read_root():
    return {"Message":"Finnaly I am able to run my first API"}





