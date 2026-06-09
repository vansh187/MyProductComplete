from database.loginPersist import LoginPersistence
from utils.password_hasher import verify_password
from utils.jwt_handler  import create_access_token
class LoginService:
    def __init__(self):
        self._persistence = LoginPersistence()

    def loginUser(self, login_request):
        # Hash the password before checking it against the database
        user=self._persistence.login_user(login_request)
        if user is None:
            raise ValueError("Invalid credentials")
        
        elif not verify_password(login_request.password, user[4]):
            raise ValueError("Invalid credentials")
        else:
            usertoken = create_access_token({
        "sub": login_request.email,
         "user_id": user[0] # subject = user identity
            })
            return usertoken