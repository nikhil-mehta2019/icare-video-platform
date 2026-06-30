from io import BytesIO
import pandas as pd
from app.database.session import SessionLocal
from app.database.models import Video, MigrationJob, MigrationError


def generate_migration_excel(title_suffix: str = None, job_id: int = None,
                             job_ids: list[int] = None) -> BytesIO:
    """
    Exports Video records to a multi-sheet Excel file.
    Sheets: All Videos | one per folder | Failed Videos

    title_suffix : filter by display_title suffix e.g. " (New_Romance)"
    job_id       : filter to only videos migrated during a single job (by created_at window)
    job_ids      : filter to videos migrated across multiple jobs (union of their time windows)
                   Supersedes job_id when provided.
    """
    with SessionLocal() as db:
        q = db.query(Video).order_by(Video.created_at)
        if title_suffix:
            q = q.filter(Video.display_title.like(f"%{title_suffix}"))

        # Multi-job filter: union the time windows for all requested jobs
        if job_ids:
            from sqlalchemy import or_
            job_records = db.query(MigrationJob).filter(MigrationJob.id.in_(job_ids)).all()
            job_records.sort(key=lambda j: j.created_at)
            # Build a set of all job IDs so we can pull errors too
            all_job_ids = [j.id for j in job_records]
            if job_records:
                # Collect all error job_ids for later; build time-window OR for videos
                windows = []
                all_jobs_sorted = sorted(db.query(MigrationJob).all(), key=lambda j: j.id)
                all_job_map = {j.id: j for j in all_jobs_sorted}
                for jr in job_records:
                    # find the next job chronologically (by id) after this one
                    next_j = next((j for j in all_jobs_sorted if j.id > jr.id), None)
                    if next_j:
                        windows.append(
                            (Video.created_at >= jr.created_at) &
                            (Video.created_at < next_j.created_at)
                        )
                    else:
                        windows.append(Video.created_at >= jr.created_at)
                q = q.filter(or_(*windows))
            eq = db.query(MigrationError).filter(
                MigrationError.job_id.in_(all_job_ids)
            ).order_by(MigrationError.created_at)
        elif job_id:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            if job:
                q = q.filter(Video.created_at >= job.created_at)
                next_job = db.query(MigrationJob).filter(
                    MigrationJob.id > job_id
                ).order_by(MigrationJob.id).first()
                if next_job:
                    q = q.filter(Video.created_at < next_job.created_at)
            eq = db.query(MigrationError).filter(
                MigrationError.job_id == job_id
            ).order_by(MigrationError.created_at)
        else:
            eq = db.query(MigrationError).order_by(MigrationError.created_at)

        videos = q.all()
        errors = eq.all()

    rows = []
    for v in videos:
        rows.append({
            "Source": v.source or "vimeo",
            "DB ID": v.vimeo_id,
            "Title": v.vimeo_title,
            "Display Title (Mux)": v.display_title or v.vimeo_title,
            "Folder": v.vimeo_folder_path or "Root",
            "Vimeo URL": v.vimeo_url or "",
            "Mux Asset ID": v.mux_asset_id or "",
            "Mux Playback ID (Public)": v.mux_playback_id or "",
            "Mux Signed Playback ID": v.mux_signed_playback_id or "",
            "Mux DRM Playback ID": v.mux_drm_playback_id or "",
            "Mux Stream URL": v.mux_stream_url or "",
            "Captions Count": v.captions_count,
            "Caption Languages": v.captions_languages or "",
            "Audio Tracks Count": v.audio_tracks_count,
            "Audio Languages": v.audio_languages or "",
            "Status": v.status,
            "Migrated At": v.created_at.strftime("%Y-%m-%d %H:%M:%S") if v.created_at else "",
        })

    df_all = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
        "Source", "DB ID", "Title", "Display Title (Mux)", "Folder", "Vimeo URL",
        "Mux Asset ID", "Mux Playback ID (Public)", "Mux DRM Playback ID", "Mux Stream URL",
        "Captions Count", "Caption Languages", "Audio Tracks Count", "Audio Languages",
        "Status", "Migrated At",
    ])

    error_rows = [{
        "DB ID / Vimeo ID": e.vimeo_id,
        "Error Message": e.error_message,
        "Failed At": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
    } for e in errors]
    df_errors = pd.DataFrame(error_rows)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        _write_sheet(writer, df_all, "All Videos")

        for folder in sorted(df_all["Folder"].unique()):
            df_folder = df_all[df_all["Folder"] == folder].copy()
            sheet_name = folder[:31].translate(str.maketrans("", "", r'\/:*?[]'))
            _write_sheet(writer, df_folder, sheet_name or "Root")

        if not df_errors.empty:
            _write_sheet(writer, df_errors, "Failed Videos")

    output.seek(0)
    return output


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, name: str):
    df.to_excel(writer, index=False, sheet_name=name)
    ws = writer.sheets[name]
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)
