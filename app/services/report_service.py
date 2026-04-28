import pandas as pd
from io import BytesIO
from app.database.session import SessionLocal
from app.database.models import Video, MigrationError

def generate_migration_excel() -> BytesIO:
    """Queries the database and returns an in-memory Excel file of the migration mapping,
    with one sheet per Vimeo folder and a combined 'All Videos' sheet."""
    db = SessionLocal()
    try:
        videos = db.query(Video).all()

        rows = []
        for v in videos:
            rows.append({
                "Vimeo ID": v.vimeo_id,
                "Vimeo Title": v.vimeo_title,
                "Vimeo Folder Path": v.vimeo_folder_path or "Root",
                "Vimeo URL": v.vimeo_url,
                "Mux Asset ID": v.mux_asset_id,
                "Mux Playback ID": v.mux_playback_id,
                "Mux Player URL": f"https://player.mux.com/{v.mux_playback_id}" if v.mux_playback_id else "",
                "Mux Signed Playback ID": v.mux_signed_playback_id or "",
                "Mux DRM Playback ID": v.mux_drm_playback_id or "",
                "Mux Stream URL": v.mux_stream_url,
                "Captions Count": v.captions_count,
                "Captions Languages": v.captions_languages or "None",
                "Audio Tracks Count": v.audio_tracks_count,
                "Audio Languages": v.audio_languages or "None",
                "Migrated At": v.created_at.strftime("%Y-%m-%d %H:%M:%S") if v.created_at else "",
            })

        df_all = pd.DataFrame(rows)

        errors = db.query(MigrationError).all()
        error_rows = [
            {
                "Vimeo ID": e.vimeo_id,
                "Error Message": e.error_message,
                "Failed At": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
            }
            for e in errors
        ]
        df_errors = pd.DataFrame(error_rows)

        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            _write_sheet(writer, df_all, "All Videos")

            for folder in sorted(df_all["Vimeo Folder Path"].unique()):
                df_folder = df_all[df_all["Vimeo Folder Path"] == folder].copy()
                sheet_name = folder[:31].translate(str.maketrans('', '', r'\/:*?[]'))
                _write_sheet(writer, df_folder, sheet_name)

            if not df_errors.empty:
                _write_sheet(writer, df_errors, "Failed Videos")

        output.seek(0)
        return output
    finally:
        db.close()


def _write_sheet(writer: pd.ExcelWriter, df: pd.DataFrame, sheet_name: str):
    df.to_excel(writer, index=False, sheet_name=sheet_name)
    worksheet = writer.sheets[sheet_name]
    for col in worksheet.columns:
        max_length = max(
            (len(str(cell.value)) for cell in col if cell.value is not None),
            default=10,
        )
        worksheet.column_dimensions[col[0].column_letter].width = min(max_length + 2, 60)
