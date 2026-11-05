import csv
import gzip
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from mixed_trip_modes import report, scan, straight_km


class MixedTripTests(unittest.TestCase):
    def test_distance_valid_and_missing(self):
        row=dict(LATITUDE_START='0', LONGITUDE_START='0', LATITUDE_END='0', LONGITUDE_END='1')
        self.assertAlmostEqual(straight_km(row),111.195,places=2)
        row['LONGITUDE_END']='null'
        self.assertIsNone(straight_km(row))

    def test_repeats_and_unknown_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            base=dict(DAILYID='a',TRIP_RK='1',LOGTIME_START='2025-11-01T08:00:00+09:00',
                      LOGTIME_END='2025-11-01T08:30:00+09:00',YEAR='2025',MONTH='11',DAY='1',HOUR='8',
                      TRAFFIC_MODE='5',LATITUDE_START='0',LONGITUDE_START='0',LATITUDE_END='0',LONGITUDE_END='1')
            rows=[dict(base,TRIP_RK=k,TRAFFIC_MODE=m) for k,m in [('1','5'),('1','5'),('1','8'),('2','5'),('2','9'),('3','8'),('3','8')]]
            import io
            stream=io.StringIO(newline='')
            writer=csv.DictWriter(stream,fieldnames=list(base))
            writer.writeheader()
            writer.writerows(rows)
            source=out/'input.zip'
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('point/20251101/data.csv.gz',gzip.compress(stream.getvalue().encode()))
            with sqlite3.connect(':memory:') as db:
                stats=scan(SimpleNamespace(input=source,limit=0),db)
                report(db,out,stats,0)
            result=json.loads((out/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(result['total_trips'],3)
            self.assertEqual(result['repeated_trips'],3)
            self.assertEqual(result['mixed_code_trips'],2)
            self.assertEqual(result['multiple_known_mode_trips'],1)
            self.assertAlmostEqual(result['mean_minutes'],30)
            self.assertAlmostEqual(result['mean_straight_km'],111.195,places=2)


if __name__=='__main__':
    unittest.main()
