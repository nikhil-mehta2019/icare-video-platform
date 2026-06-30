import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from app.database.session import SessionLocal
from app.database.models import Video

with SessionLocal() as db:
    v = db.query(Video).filter(Video.mux_asset_id == '6ZBDtRGm01t6tlSg4F4LVfGz8EnK83V3aq4syn463KmM').first()
    langs = [l.strip() for l in (v.captions_languages or '').split(',') if l.strip()]
    if 'es' not in langs:
        langs.append('es')
        v.captions_languages = ','.join(langs)
        v.captions_count = len(langs)
        db.commit()
    print('Done:', v.vimeo_title, '| captions:', v.captions_languages)
