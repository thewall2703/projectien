from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from backend.seed import _asset_sheet_rows


class AssetSheetRowsTests(unittest.TestCase):
    def test_uses_drive_video_column_and_shifted_photo_report_columns(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Videos  Photos  Reports"
        sheet.append(["Videos (YouTube)", "Videos (Drive)", "", "Photos", "Reports"])
        sheet.append(["YouTube title", "Drive title", "note", "Photo title", "Report title"])
        sheet["A2"].hyperlink = "https://www.youtube.com/watch?v=abc"
        sheet["B2"].hyperlink = "https://drive.google.com/file/d/video-id/view"
        sheet["D2"].hyperlink = "https://drive.google.com/drive/folders/photo-id"
        sheet["E2"].hyperlink = "https://example.com/report.pdf"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assets.xlsx"
            workbook.save(path)
            rows = _asset_sheet_rows(path)

        self.assertEqual(
            rows[0],
            [
                ("Drive title", "https://drive.google.com/file/d/video-id/view"),
                ("Photo title", "https://drive.google.com/drive/folders/photo-id"),
                ("Report title", "https://example.com/report.pdf"),
            ],
        )

    def test_ignores_video_without_drive_hyperlink(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Videos  Photos  Reports"
        sheet.append(["Videos (YouTube)", "Videos (Drive)", "", "Photos", "Reports"])
        sheet.append(["YouTube only", "Text without link", "", "", ""])
        sheet["A2"].hyperlink = "https://www.youtube.com/watch?v=abc"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assets.xlsx"
            workbook.save(path)
            rows = _asset_sheet_rows(path)

        self.assertEqual(rows[0][0], ("", ""))


if __name__ == "__main__":
    unittest.main()
