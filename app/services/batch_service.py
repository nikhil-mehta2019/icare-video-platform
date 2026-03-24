import pandas as pd
import io
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.database.models import User, Course, UserCourseAccess

logger = logging.getLogger(__name__)

def process_batch_csv(db: Session, file_content: bytes, course_id: int):
    # Read the CSV from memory
    df = pd.read_csv(io.BytesIO(file_content))
    
    # Standardize column names (lowercase, strip whitespace)
    df.columns = df.columns.str.strip().str.lower()
    
    if "email" not in df.columns or "name" not in df.columns:
        raise ValueError("CSV must contain 'name' and 'email' columns.")

    # Ensure the target course exists (Create a default if it doesn't)
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        course = Course(id=course_id, title="Caregiver Onboarding", description="Standard 90-day training")
        db.add(course)
        db.commit()

    added_users = 0
    updated_access = 0

    for index, row in df.iterrows():
        email = str(row["email"]).strip()
        name = str(row["name"]).strip()

        if not email or pd.isna(email) or email.lower() == "nan":
            continue

        # 1. Check if user already exists
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(email=email, name=name)
            db.add(user)
            db.commit()
            db.refresh(user)
            added_users += 1

        # 2. Grant or Reset 90-day access
        access = db.query(UserCourseAccess).filter(
            UserCourseAccess.user_id == user.id,
            UserCourseAccess.course_id == course_id
        ).first()

        new_end_date = datetime.utcnow() + timedelta(days=90)

        if access:
            # If they already existed, reset their 90-day clock
            access.access_start = datetime.utcnow()
            access.access_end = new_end_date
            updated_access += 1
        else:
            # Grant access for the first time
            access = UserCourseAccess(
                user_id=user.id,
                course_id=course_id,
                access_start=datetime.utcnow(),
                access_end=new_end_date
            )
            db.add(access)

    db.commit()
    return {
        "status": "success", 
        "new_accounts_created": added_users, 
        "total_access_granted": added_users + updated_access
    }