import pandas as pd
from datetime import datetime, timedelta

from app.database.db import SessionLocal
from app.database.models import User, UserCourseAccess


def import_batch(file_path, course_id):

    # detect file type
    if file_path.endswith(".csv"):
        df = pd.read_csv(file_path)
    elif file_path.endswith(".xlsx") or file_path.endswith(".xls"):
        df = pd.read_excel(file_path)
    else:
        raise ValueError("Unsupported file format")

    # remove duplicate emails in file
    df = df.drop_duplicates(subset=["email"])

    db = SessionLocal()

    new_users = 0
    updated_users = 0

    try:

        for _, row in df.iterrows():

            expiry = datetime.utcnow() + timedelta(days=90)

            user = db.query(User).filter(User.email == row["email"]).first()

            # create user if not exists
            if not user:
                user = User(
                    email=row["email"],
                    name=row["name"]
                )
                db.add(user)
                db.commit()
                db.refresh(user)
                new_users += 1
            else:
                updated_users += 1

            # check if course access already exists
            access = db.query(UserCourseAccess).filter(
                UserCourseAccess.user_id == user.id,
                UserCourseAccess.course_id == course_id
            ).first()

            if access:
                # update expiry
                access.access_end = expiry
            else:
                access = UserCourseAccess(
                    user_id=user.id,
                    course_id=course_id,
                    access_start=datetime.utcnow(),
                    access_end=expiry
                )
                db.add(access)

        db.commit()

        return {
            "status": "batch processed",
            "new_users": new_users,
            "updated_users": updated_users
        }

    finally:
        db.close()