from database.loginPersist import LoginPersistence
from utils.password_hasher import verify_password
from utils.jwt_handler import create_access_token


class LoginService:

    def __init__(self):
        self._persistence = LoginPersistence()

    def loginUser(self, login_request):
        user = self._persistence.login_user(login_request)
        if user is None:
            raise ValueError("Invalid credentials")
        elif not verify_password(login_request.password, user['password']):
            raise ValueError("Invalid credentials")
        else:
            usertoken = create_access_token({
                "sub": login_request.email,
                "user_id": user['user_id']
            })
            return usertoken
