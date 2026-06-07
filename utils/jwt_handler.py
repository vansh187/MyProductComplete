from jose import JWTError, jwt
from datetime import datetime, timedelta

def create_access_token(data: dict):
    SECRET_KEY = "mysecretkey123"
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 30
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    SECRET_KEY = "mysecretkey123"
    ALGORITHM = "HS256"
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload  # contains user info (email, exp)
    except JWTError:
        return None
def decode_token(token: str):
    SECRET_KEY = "mysecretkey123"
    ALGORITHM = "HS256" 
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload  # contains user info (email, exp)
    except JWTError:
        raise Exception("JWT decoding error"+str(JWTError))
    except jwt.ExpiredSignatureError:
            raise Exception("Token expired"+str(jwt.ExpiredSignatureError))
    except jwt.InvalidTokenError:
            raise Exception("Invalid token"+str(jwt.InvalidTokenError))    

def get_user_from_token(request):
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise Exception("Authorization header missing")

    token = auth_header.replace("Bearer ", "")
    payload = decode_token(token)

    return payload
