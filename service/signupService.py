from database.signUpPersist import signUpPersist
from utils.password_hasher import hash_password

class SignupService:
    def __init__(self):
        self.signUpPersist = signUpPersist()

    def signupUser(self, signup_request):
        # Hash the password before storing it in the database
        signup_request.password = hash_password(signup_request.password)
        self.signUpPersist.signup_user(signup_request)


   