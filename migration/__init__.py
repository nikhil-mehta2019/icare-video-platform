"""Vimeo Folder 1 -> Mux migration path.

Self-contained and additive: it reuses app.config, app.services.vimeo_service
(folder enumeration) and the existing yt-dlp approach, but keeps its own
state tables/database and never touches the production `videos` table,
Base44, or any existing Mux asset.
"""
