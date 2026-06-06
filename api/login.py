from fastapi import APIRouter
from pydantic import BaseModel
from service.loginService import LoginService

router=APIRouter()

class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(login_request:LoginRequest):
    loginService = LoginService()

    try:
        usertoken = loginService.loginUser(login_request)
        if usertoken is None:
            return {"Message": "Invalid email or password"}
        else:   
            return {"Message": f"User with email {login_request.email} has been successfully logged in.", "Token": usertoken, "TokenType": "Bearer"}
    except Exception as e:
        return {"Message": f"An error occurred during login: {str(e)}"}

