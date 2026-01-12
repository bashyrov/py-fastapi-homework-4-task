from typing import Annotated

from fastapi import (APIRouter,
                     Depends,
                     Request,
                     HTTPException,
                     UploadFile,
                     File)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from starlette.responses import JSONResponse

from config import get_jwt_auth_manager, get_s3_storage_client
from exceptions import (InvalidTokenError,
                        TokenExpiredError,
                        S3FileUploadError)
from security.token_manager import JWTAuthManager
from schemas import ProfileCreate, ProfileCreateResponseSchema
from database import (get_db,
                      UserProfileModel,
                      UserModel,
                      UserGroupEnum)
from storages import S3StorageInterface

router = APIRouter()


async def get_current_user(
        request: Request,
        jwt_manager: JWTAuthManager = Depends(get_jwt_auth_manager),
        db: AsyncSession = Depends(get_db)
) -> UserModel | JSONResponse:

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header is missing")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'",
        )

    token = auth_header.split("Bearer ")[1]

    try:
        payload = jwt_manager.decode_access_token(token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=401, detail="Token has expired."
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=401, detail="Invalid token."
        )

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=401, detail="Invalid token."
        )

    user = await db.scalar(
        select(UserModel)
        .where(UserModel.id == user_id)
        .options(joinedload(UserModel.group))
    )
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="User not found or not active.",
        )

    return user


async def upload_avatar(
        user_id: int,
        s3_client: S3StorageInterface,
        avatar_file: UploadFile = File(...),

) -> str:

    avatar_byte_data = await avatar_file.read()
    avatar_path = f"avatars/{user_id}_{avatar_file.filename}"

    try:
        await s3_client.upload_file(file_name=avatar_path, file_data=avatar_byte_data)

        return avatar_path
    except S3FileUploadError:
        raise HTTPException(
            status_code=500,
            detail="Failed to upload avatar. Please try again later.",
        )


@router.post(
    path="/users/{user_id}/profile/",
    response_model=ProfileCreateResponseSchema,
    status_code=201
)
async def create_profile(
        user_id: int,
        profile_data: Annotated[
            ProfileCreate, Depends(ProfileCreate.as_form)
        ],
        db: AsyncSession = Depends(get_db),
        avatar: UploadFile = File(...),
        current_user: UserModel = Depends(get_current_user),
        s3_client: S3StorageInterface = Depends(get_s3_storage_client),
):
    stmt = select(UserModel).options(joinedload(UserModel.profile)).where(UserModel.id == user_id)
    result = await db.execute(stmt)
    user = result.scalars().first()

    if not user or user.is_active is False:
        raise HTTPException(
            status_code=401,
            detail="User not found or not active."
        )

    if user_id != current_user.id and not current_user.has_group(UserGroupEnum.ADMIN):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to edit this profile."
        )

    if user.profile:
        raise HTTPException(
            status_code=400,
            detail="User already has a profile."
        )

    avatar_byte_data = await profile_data.avatar.read()
    avatar_path = f"avatars/{user.id}_{profile_data.avatar.filename}"

    try:
        await s3_client.upload_file(file_name=avatar_path, file_data=avatar_byte_data)
    except S3FileUploadError:
        raise HTTPException(
            status_code=500,
            detail="Failed to upload avatar. Please try again later.",
        )

    profile = UserProfileModel(
        **profile_data.model_dump(exclude=["avatar"]),
        avatar=avatar_path,
        user_id=user.id,
    )

    db.add(profile)
    await db.commit()
    avatar_url = await s3_client.get_file_url(profile.avatar)

    return ProfileCreateResponseSchema(
        id=profile.id,
        user_id=profile.user_id,
        first_name=profile.first_name,
        last_name=profile.last_name,
        gender=profile.gender,
        date_of_birth=profile.date_of_birth,
        info=profile.info,
        avatar=avatar_url,
    )
