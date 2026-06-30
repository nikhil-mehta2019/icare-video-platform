import unittest
from pathlib import Path

from app.services.bulk_attachment_service import normalize_name, find_matching_files


class BulkAttachmentServiceTests(unittest.TestCase):
    def test_normalize_name_strips_suffixes_and_spaces(self):
        self.assertEqual(normalize_name("My Video Hindi SRT.srt"), "my_video")
        self.assertEqual(normalize_name("My Video VO Hindi.mp3"), "my_video")
        self.assertEqual(normalize_name("My Video.mp4"), "my_video")

    def test_find_matching_files_prefers_exact_and_fallback(self):
        root = Path(__file__).parent
        srt_dir = root / "fixtures" / "srt"
        audio_dir = root / "fixtures" / "audio"

        result = find_matching_files("My Video.mp4", srt_dir, audio_dir)
        self.assertEqual(result["srt"], srt_dir / "my_video.srt")
        self.assertEqual(result["audio"], audio_dir / "my_video.mp3")


if __name__ == "__main__":
    unittest.main()
