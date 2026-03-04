from fastapi import APIRouter, HTTPException, status
from datetime import datetime, timedelta
from jose import jwt
import os
import pymysql

from models import AuthTokenResponse, LoginRequest, SetPasswordRequest, UserCreate
from .mysql_store import create_user, get_user_by_username, update_user_password_and_status
from .security import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["Auth"])

SECRET_KEY = os.getenv("JWT_SECRET", "dev-secret")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "1"))

# /register endpoint flow:
# # register new patient
'''
forward the request to /register,
send a request to the EHR service (check permission; only doctors can create new patients),
use the UUID from the EHR service to register the new patient in the auth-service set user-status = "PENDING",
navigate to /set-password endpoint, set user-status = "REGISTERED",
Navigate to /login and allow the new user to access the system and their own records in EHR-service,
'''

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserCreate):
    try:
        existing = get_user_by_username(user.userName)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already exists",
            )

        create_user(
            username=user.userName,
            password_hash=hash_password(user.password),
            doctor_id=user.doctorID,
            patient_id=user.patientID,
            role=user.role.value,
            user_status=user.userStatus.value,
        )
    except pymysql.MySQLError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth database unavailable: {exc}",
        ) from exc

    return {
        "userName": user.userName,
        "role": user.role.value,
        "userStatus": user.userStatus.value,
    }


@router.post("/login", response_model=AuthTokenResponse)
def login(payload: LoginRequest):
    try:
        user = get_user_by_username(payload.userName)
    except pymysql.MySQLError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth database unavailable: {exc}",
        ) from exc

    if user is None or not verify_password(payload.password, str(user["password_hash"])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    role = str(user["role"])
    token_payload = {
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }

    if role == "doctor" and user.get("doctor_id"):
        token_payload["doctor_id"] = user["doctor_id"]
    if role == "patient" and user.get("patient_id"):
        token_payload["patient_id"] = user["patient_id"]
        # keep compatibility with existing access checks that look for patient_uuid
        token_payload["patient_uuid"] = user["patient_id"]

    token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)

    patient_id = str(user["patient_id"]) if role == "patient" and user.get("patient_id") else None
    user_status = str(user["user_status"])

    return AuthTokenResponse(
        access_token=token,
        role=role,
        patientID=patient_id,
        userStatus=user_status,
    )


@router.post("/set-password", status_code=status.HTTP_200_OK)
def set_password(payload: SetPasswordRequest):
    """
    Allows a PENDING patient account to set a new password.
    After a successful update the account status is changed to REGISTERED.
    Only works for patient accounts whose current status is 'pending'.
    """
    try:
        user = get_user_by_username(payload.userName)
    except pymysql.MySQLError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth database unavailable: {exc}",
        ) from exc

    if user is None or not verify_password(payload.currentPassword, str(user["password_hash"])):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your current password is incorrect. Please provide the correct temporary password to set a new password.",
        )

    if str(user["role"]) != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only patient accounts can change their temporary password.",
        )

    if str(user["user_status"]) != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account is not in PENDING status. Password has already been set.",
        )

    new_hash = hash_password(payload.newPassword)

    try:
        update_user_password_and_status(
            username=payload.userName,
            new_password_hash=new_hash,
            new_status="registered",
        )
    except pymysql.MySQLError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Auth database unavailable: {exc}",
        ) from exc

    return {
        "message": "Password updated successfully. Account is now REGISTERED.",
        "userName": payload.userName,
        "userStatus": "registered",
    }

