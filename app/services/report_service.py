import pandas as pd
from io import BytesIO
from app.database.session import SessionLocal
from app.database.models import Video

def generate_migration_excel() -> BytesIO:
    """Queries the database and returns an in-memory Excel file of the mapping."""
    db = SessionLocal()
    try:
        videos = db.query(Video).all()
        
        data = []
        for v in videos:
            data.append({
                "Vimeo Title": v.vimeo_title,
                "Vimeo Folder Path": v.vimeo_folder_path or "Root",
                "Vimeo URL": v.vimeo_url,
                "Mux Title": v.vimeo_title,
                "Mux Asset ID": v.mux_asset_id,
                "Mux Playback URL": v.mux_stream_url
            })

        df = pd.DataFrame(data)
        
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Migration Mapping')
            
            # Auto-adjust column widths for readability
            worksheet = writer.sheets['Migration Mapping']
            for col in worksheet.columns:
                max_length = 0
                column = col[0].column_letter
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(cell.value)
                    except:
                        pass
                adjusted_width = (max_length + 2)
                worksheet.column_dimensions[column].width = adjusted_width

        output.seek(0)
        return output
    finally:
        db.close()