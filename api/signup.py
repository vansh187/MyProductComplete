from pydantic import BaseModel
from fastapi import APIRouter
from service.signupService import SignupService


class SignupRequest(BaseModel):
    first_name: str
    last_name: str
    email: str
    password: str
    phone_number: str


router = APIRouter()


@router.post("/signup")
def signup(signup_request: SignupRequest):
    signup_service = SignupService()
    try:
        signup_service.signupUser(signup_request)
        return {"Message": f"User {signup_request.first_name} {signup_request.last_name} with email {signup_request.email} has been successfully registered."}
    except Exception as e:
        return {"Message": f"An error occurred during signup: {str(e)}"}


