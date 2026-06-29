from fastapi import APIRouter
from pydantic import BaseModel
from service.googleAuthService import GoogleAuthService

router = APIRouter()
_service = GoogleAuthService()


class GoogleAuthRequest(BaseModel):
    id_token: str


@router.post("/auth/google")
def google_login(body: GoogleAuthRequest):
    try:
        result = _service.authenticate(body.id_token)
        return result
    except Exception as e:
        return {"Message": f"Google authentication failed: {str(e)}"}
