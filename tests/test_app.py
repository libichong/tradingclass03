import tempfile
import unittest
from pathlib import Path

import duckdb

from main import create_app, metadata, parse_time


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with duckdb.connect() as db:
            db.execute("""
                CREATE TABLE records AS SELECT * FROM (VALUES
                (1, 92500000, 18.48::FLOAT, 200, 9007199254740993::BIGINT),
                (601688, 93000123, 18.50::FLOAT, 100, 2::BIGINT),
                (601688, 93000124, NULL, 300, 3::BIGINT)
                ) t(Symbol, Time, Price, Volume, OrderNumber)
            """)
            for kind in ("exe", "snp", "ord"):
                path = str(self.root / f"{kind}_20260923.parquet").replace("'", "''")
                db.execute(f"COPY records TO '{path}' (FORMAT PARQUET)")
        self.app = create_app(self.root)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        metadata.cache_clear()
        self.temp.cleanup()

    def query(self, **params):
        return self.client.get("/api/data", query_string={"file": "exe_20260923.parquet", **params})

    def test_page_and_static_assets(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_catalog_and_all_kinds(self):
        files = self.client.get("/api/files").get_json()["files"]
        self.assertEqual(len(files), 3)
        self.assertEqual(sum(item["total"] for item in files), 9)
        for item in files:
            result = self.client.get("/api/data", query_string={"file": item["name"]}).get_json()
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["rows"][0]["OrderNumber"], "9007199254740993")
            self.assertIsNone(result["rows"][2]["Price"])

    def test_pagination_and_clamping(self):
        first = self.query(page_size=2).get_json()
        second = self.query(page_size=2, page=2).get_json()
        self.assertEqual(len(first["rows"]), 2)
        self.assertEqual(len(second["rows"]), 1)
        self.assertEqual(second["rows"][0]["Volume"], 300)
        self.assertEqual(self.query(page=99, page_size=2).get_json()["page"], 2)

    def test_symbol_and_inclusive_millisecond_time(self):
        data = self.query(symbol="601688", start="09:30:00.123", end="09:30:00.123").get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["Volume"], 100)
        self.assertEqual(self.query(symbol="000001").get_json()["total"], 1)
        self.assertEqual(parse_time("09:30:00.1"), 93000100)

    def test_empty_results(self):
        data = self.query(symbol="999999").get_json()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["pages"], 1)

    def test_bad_inputs(self):
        for params in (
            {"page": "abc"}, {"page": "0"}, {"page_size": "501"},
            {"page_size": "1.5"}, {"symbol": "1"}, {"symbol": "' OR 1=1"},
            {"start": "25:00:00"}, {"start": "09:60:00"}, {"end": "garbage"},
            {"start": "10:00:00", "end": "09:00:00"},
            {"file": "..\\secret.parquet"}, {"file": "missing.parquet"},
        ):
            with self.subTest(params=params):
                response = self.query(**params)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())

    def test_missing_directory_and_corrupt_file(self):
        app = create_app(self.root / "missing")
        with self.assertLogs(app.logger, level="ERROR"):
            self.assertEqual(app.test_client().get("/api/files").status_code, 503)
        (self.root / "exe_broken.parquet").write_text("broken", encoding="utf-8")
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.query(file="exe_broken.parquet")
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.get_json())

    def test_empty_directory_and_ignored_files(self):
        for path in self.root.iterdir():
            path.unlink()
        (self.root / "notes.txt").write_text("not market data", encoding="utf-8")
        self.assertEqual(self.client.get("/api/files").get_json(), {"files": []})

    def test_replaced_file_invalidates_metadata(self):
        before = self.client.get("/api/files").get_json()["files"][0]["total"]
        path = str(self.root / "exe_20260923.parquet").replace("'", "''")
        with duckdb.connect() as db:
            db.execute(f"COPY (SELECT 1 AS Symbol) TO '{path}' (FORMAT PARQUET)")
        after = self.client.get("/api/files").get_json()["files"][0]["total"]
        self.assertEqual((before, after), (3, 1))


if __name__ == "__main__":
    unittest.main()
