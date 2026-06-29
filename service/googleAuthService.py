import os
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from dotenv import load_dotenv

from database.googleAuthPersistence import GoogleAuthPersistence
from utils.jwt_handler import create_access_token

load_dotenv()

_persistence = GoogleAuthPersistence()


class GoogleAuthService:

    def authenticate(self, token: str) -> dict:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        if not client_id:
            raise Exception("GOOGLE_CLIENT_ID is not configured")

        try:
            claims = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                client_id
            )
        except ValueError as e:
            raise Exception(f"Invalid Google token: {str(e)}")

        email = claims.get("email")
        if not email:
            raise Exception("Google token does not contain an email address")

        first_name = claims.get("given_name", "")
        last_name = claims.get("family_name", "")

        user = _persistence.get_user_by_email(email)
        if user is None:
            user_id = _persistence.create_google_user(first_name, last_name, email)
        else:
            user_id = user["user_id"]

        jwt = create_access_token({"sub": email, "user_id": user_id})
        return {
            "Message": f"User {email} authenticated via Google.",
            "Token": jwt,
            "TokenType": "Bearer"
        }
