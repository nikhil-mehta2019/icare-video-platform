from datetime import datetime
from app.database.db import SessionLocal
from app.database.models import User, UserCourseAccess


def check_access(email, course_id):

    db = SessionLocal()

    try:

        user = db.query(User).filter(User.email == email).first()

        if not user:
            return False

        access = db.query(UserCourseAccess).filter(
            UserCourseAccess.user_id == user.id,
            UserCourseAccess.course_id == course_id
        ).first()

        if not access:
            return False

        if datetime.utcnow() > access.access_end:
            return False

        return True

    finally:
        db.close()