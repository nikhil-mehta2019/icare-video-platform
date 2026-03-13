iCare Video Training Platform

Backend system for managing caregiver training videos, batch onboarding, and secure video streaming using Mux.

This platform automates caregiver onboarding, enforces 90-day access control, and enables secure mobile offline viewing for training programs.

Project Purpose

The system replaces Vimeo OTT subscriber management with a fully controlled backend that:

Automates caregiver onboarding from CSV batches

Provides secure video streaming via Mux

Enforces a 90-day access expiry ("kill switch")

Supports mobile offline viewing

Prevents unauthorized video sharing

Integrates with Wix/LMS platform

System Architecture
Admin Dashboard (Wix)
        │
        ▼
FastAPI Backend (Python)
        │
        ├── Database (SQLite / PostgreSQL)
        │
        └── Mux Video Platform
                │
                └── Secure Video Streaming

The backend acts as the control layer that manages user access and communicates with Mux.

Key Features
1. Video Hosting

Videos are uploaded and processed by Mux for secure streaming.

2. Batch Onboarding

Caregivers are onboarded using CSV batch uploads.

3. Access Control

Each caregiver receives 90 days of access to training content.

4. Secure Playback

Video playback is controlled using signed Mux playback URLs.

5. Offline Viewing

Mux DRM allows caregivers to download videos securely on mobile devices.

6. Automated Expiry

Access automatically expires after the training period.

Technology Stack
Component	Technology
Backend Framework	FastAPI
Database	SQLite (dev) / PostgreSQL (production)
ORM	SQLAlchemy
Video Streaming	Mux
Authentication	JWT
Data Processing	Pandas
Deployment	Docker / Cloud
Project Structure
icare-video-platform
│
├── app
│   ├── main.py
│   ├── config.py
│
│   ├── database
│   │   ├── db.py
│   │   └── models.py
│
│   ├── services
│   │   ├── mux_service.py
│   │   ├── batch_service.py
│   │   └── access_service.py
│
│   ├── routes
│   │   └── playback.py
│
│   └── utils
│       └── expiry.py
│
├── requirements.txt
├── .env
└── README.md
Installation
1. Clone the repository
git clone https://github.com/your-repo/icare-video-platform.git
cd icare-video-platform
2. Create virtual environment
python -m venv venv

Activate it:

Windows

venv\Scripts\activate

Mac/Linux

source venv/bin/activate
3. Install dependencies
pip install -r requirements.txt
Environment Variables

Create .env

MUX_TOKEN_ID=your_mux_token
MUX_TOKEN_SECRET=your_mux_secret

DATABASE_URL=sqlite:///icare.db
JWT_SECRET=icare-secret
Running the Server

Start the backend:

uvicorn app.main:app --reload

Server will run at:

http://localhost:8000

API documentation:

http://localhost:8000/docs
Database

The system currently uses SQLite for development.

Database file:

icare.db

Tables include:

Table	Purpose
users	caregiver accounts
videos	Mux video mapping
batches	agency training batches
user_access	access control with expiry
logs	onboarding logs

Production deployments should switch to PostgreSQL.

Video Playback

Videos are streamed using Mux.

Example playback URL:

https://stream.mux.com/{playback_id}.m3u8

Playback access is validated through the backend before returning the streaming URL.

Batch Onboarding Workflow
Admin uploads CSV
      │
      ▼
Backend processes caregivers
      │
      ▼
Accounts created in database
      │
      ▼
Access assigned (90 days)
      │
      ▼
Caregivers receive login access
Access Control Logic

Each user receives a time-limited access window.

Access Start = registration date
Access End = start + 90 days

If the current date exceeds the access period, video playback is blocked.

Offline Viewing

Mux DRM enables secure offline viewing.

Downloaded videos:

are encrypted

tied to the user device

automatically expire after access expiry

Future Enhancements

Planned improvements include:

CSV batch onboarding API

signed playback tokens

mobile DRM license service

analytics dashboard

admin batch management interface

Docker deployment

AWS / cloud hosting

Development Roadmap

Video migration from Vimeo to Mux

Batch onboarding API

Playback security tokens

Offline DRM integration

Production deployment

License

Internal project for iCare caregiver training platform.