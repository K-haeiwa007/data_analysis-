"""考察の分母・空データ・部分集計の扱いを確認する。"""
import sqlite3
import unittest

from data import interpretation


class InterpretationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE points(day TEXT,hour INTEGER,mode TEXT,city TEXT,count INTEGER);
            CREATE TABLE users(day TEXT,id TEXT);
            CREATE TABLE trips(day TEXT,mode TEXT);
        ''')
        self.summary = {'points': 0, 'trips': 0, 'sample_limit': 0}

    def tearDown(self):
        self.db.close()

    def populate(self):
        for day, count in [('2025-11-03', 10), ('2025-11-04', 10), ('2025-11-08', 15)]:
            self.db.execute('INSERT INTO points VALUES (?,8,?,?,?)', (day, '5', '<地域>', count))
            self.db.executemany('INSERT INTO trips VALUES (?,?)', [(day, '5')] * count)
            self.db.executemany('INSERT INTO users VALUES (?,?)', [(day, str(i)) for i in range(5)])
        self.summary.update(points=35, trips=35)

    def test_empty_has_no_invented_findings(self):
        text = ''.join(interpretation(self.db, self.summary))
        self.assertIn('対象データがない', text)
        self.assertNotIn('最多の推定交通手段は', text)

    def test_weekday_comparison_uses_daily_mean_and_id_denominator(self):
        self.populate()
        text = ''.join(interpretation(self.db, self.summary))
        self.assertIn('月〜金2日の日平均は10.0', text)
        self.assertIn('土日1日の日平均は15.0', text)
        self.assertIn('月〜金2.00回、土日3.00回', text)
        self.assertIn('&lt;地域&gt;', text)
        self.assertIn('自動車（その他）', text)

    def test_partial_sample_does_not_compare_weekdays(self):
        self.populate()
        self.summary['sample_limit'] = 35
        text = ''.join(interpretation(self.db, self.summary))
        self.assertIn('全期間・地域の傾向へ一般化できません', text)
        self.assertNotIn('日の日平均は', text)


if __name__ == '__main__':
    unittest.main()
