import secrets
from fastapi import APIRouter, HTTPException, Depends
from app.models import AuthRequest, AuthResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings, Settings

router = APIRouter()

TOKENS = set()
security = HTTPBearer(auto_error=False)

def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(security), settings: Settings = Depends(get_settings)):
    if not settings.auth_enabled:
        return "temp-bypass-token"
    
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
        
    token = credentials.credentials
    if token not in TOKENS:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return token

@router.post("/auth/login", response_model=AuthResponse)
def login(auth: AuthRequest, settings: Settings = Depends(get_settings)):
    if not settings.auth_enabled:
        return AuthResponse(token="temp-bypass-token")
    if not settings.app_password:
        raise HTTPException(status_code=500, detail="Server configuration error")
    if not secrets.compare_digest(auth.password, settings.app_password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    token = secrets.token_hex(32)
    TOKENS.add(token)
    return AuthResponse(token=token)
