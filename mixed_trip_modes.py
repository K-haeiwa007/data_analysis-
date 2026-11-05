"""同じトリップキーに異なる交通手段がある件数と直線距離・所要時間を調べる。"""
import argparse
import csv
import gzip
import hashlib
import html
import io
import json
import math
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

from data import MODES, duration, number, value


def straight_km(row):
    coords = [number(row[c]) for c in ('LATITUDE_START', 'LONGITUDE_START', 'LATITUDE_END', 'LONGITUDE_END')]
    if any(n is None for n in coords):
        return None
    a, b, c, d = coords
    if not (-90 <= a <= 90 and -90 <= c <= 90 and -180 <= b <= 180 and -180 <= d <= 180):
        return None
    a, b, c, d = map(math.radians, coords)
    h = math.sin((c-a)/2)**2 + math.cos(a)*math.cos(c)*math.sin((d-b)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def mode_label(code):
    return MODES.get(int(code), code) if code.isdigit() else ('欠損' if code == 'missing' else code)


def scan(args, db):
    db.executescript('''
      CREATE TABLE trips(k TEXT PRIMARY KEY,day TEXT,start TEXT,end TEXT,km REAL,minutes REAL,n INTEGER) WITHOUT ROWID;
      CREATE TABLE modes(k TEXT,mode TEXT,n INTEGER,PRIMARY KEY(k,mode)) WITHOUT ROWID;
    ''')
    stats = Counter()
    required = {'DAILYID','TRIP_RK','LOGTIME_START','LOGTIME_END','YEAR','MONTH','DAY','HOUR',
                'TRAFFIC_MODE','LATITUDE_START','LONGITUDE_START','LATITUDE_END','LONGITUDE_END'}
    with zipfile.ZipFile(args.input) as archive:
        for entry in sorted(archive.infolist(), key=lambda e: e.filename):
            if not entry.filename.lower().endswith('.csv.gz'):
                continue
            print(f'読み込み: {entry.filename}', flush=True)
            groups = {}
            with archive.open(entry) as raw, gzip.GzipFile(fileobj=raw) as gz, io.TextIOWrapper(gz, encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                if required - set(reader.fieldnames or []):
                    raise ValueError(f'必須列がありません: {sorted(required-set(reader.fieldnames or []))}')
                for row in reader:
                    stats['rows_read'] += 1
                    if None in row or any(row[c] is None for c in required):
                        stats['invalid_rows'] += 1
                        continue
                    try:
                        day = date(int(row['YEAR']),int(row['MONTH']),int(row['DAY'])).isoformat()
                        if not 0 <= int(row['HOUR']) <= 23:
                            raise ValueError()
                    except ValueError:
                        stats['invalid_rows'] += 1
                        continue
                    stats['points'] += 1
                    uid, rk, start, end = (value(row[c]) for c in ('DAILYID','TRIP_RK','LOGTIME_START','LOGTIME_END'))
                    if not all((uid,rk,start,end)):
                        stats['points_without_trip_key'] += 1
                    else:
                        key = (day,uid,rk,start,end)
                        if key not in groups:
                            groups[key] = [straight_km(row), duration(start,end), Counter()]
                        groups[key][2][value(row['TRAFFIC_MODE']) or 'missing'] += 1
                    if stats['points'] % 1000000 == 0:
                        print(f"  {stats['points']:,} ポイント処理済み", flush=True)
                    if args.limit and stats['points'] >= args.limit:
                        break
            for key, (km, minutes, modes) in groups.items():
                token = json.dumps(key, separators=(',',':'))
                day, _, _, start, end = key
                db.execute('INSERT INTO trips VALUES (?,?,?,?,?,?,?) ON CONFLICT(k) DO UPDATE SET n=n+excluded.n',
                           (token,day,start,end,km,minutes,sum(modes.values())))
                db.executemany('INSERT INTO modes VALUES (?,?,?) ON CONFLICT(k,mode) DO UPDATE SET n=n+excluded.n',
                               [(token,m,n) for m,n in modes.items()])
            db.commit()
            if args.limit and stats['points'] >= args.limit:
                break
    return stats


def report(db, out, stats, limit):
    total, duplicates = db.execute('SELECT COUNT(*),COALESCE(SUM(n>1),0) FROM trips').fetchone()
    query = '''SELECT t.k,t.day,t.start,t.end,t.km,t.minutes,t.n,
      GROUP_CONCAT(m.mode),SUM(m.mode IN ('1','2','3','4','5','6','7','8')) known
      FROM trips t JOIN modes m ON t.k=m.k GROUP BY t.k HAVING COUNT(*)>1 ORDER BY t.day,t.start,t.k'''
    combinations = Counter()
    mixed = known = distance_n = minutes_n = 0
    distance_sum = minutes_sum = 0.0
    examples = []
    with (out/'mixed_trips.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['照合用トリップハッシュ','観測日','始点時刻','終点時刻','交通手段の組み合わせ',
                         '複数の具体的な手段あり','ポイント数','始終点直線距離_km','所要時間_分'])
        for key,day,start,end,km,minutes,n,codes,valid in db.execute(query):
            mixed += 1
            known += valid >= 2
            labels = ' + '.join(mode_label(m) for m in sorted(codes.split(',')))
            combinations[labels] += 1
            if km is not None:
                distance_n += 1
                distance_sum += km
            if minutes is not None:
                minutes_n += 1
                minutes_sum += minutes
            row = [hashlib.sha256(key.encode()).hexdigest(),day,start,end,labels,'あり' if valid>=2 else 'なし',n,
                   round(km,4) if km is not None else '',round(minutes,2) if minutes is not None else '']
            writer.writerow(row)
            if len(examples)<30:
                examples.append(row[1:])
    with (out/'mode_combinations.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['交通手段の組み合わせ','トリップ数'])
        writer.writerows(combinations.most_common())
    result = dict(stats, total_trips=total, repeated_trips=duplicates, mixed_code_trips=mixed,
                  multiple_known_mode_trips=known, sample_limit=limit,
                  valid_distance_trips=distance_n, mean_straight_km=distance_sum/distance_n if distance_n else None,
                  valid_duration_trips=minutes_n, mean_minutes=minutes_sum/minutes_n if minutes_n else None)
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines = [f'対象トリップ数: {total:,} 件', f'複数ポイントに現れるトリップ: {duplicates:,} 件',
             f'交通手段コードが混在するトリップ: {mixed:,} 件',
             f'うち複数の具体的な手段（不明・欠損を除く）: {known:,} 件',
             f'混在トリップの平均直線距離: {distance_sum/distance_n:.3f} km（有効{distance_n:,}件）' if distance_n else '混在トリップの距離: 有効データなし',
             f'混在トリップの平均所要時間: {minutes_sum/minutes_n:.2f} 分（有効{minutes_n:,}件）' if minutes_n else '混在トリップの所要時間: 有効データなし']
    print('\n'+'\n'.join(lines))
    parts=['<!doctype html><html lang="ja"><meta charset="utf-8"><title>同一トリップの交通手段混在</title>',
           '<style>body{font-family:Meiryo,sans-serif;margin:35px;color:#193047}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ddd}</style>',
           '<h1>同一トリップの交通手段混在と直線距離・所要時間</h1>']
    if limit:
        parts.append('<p><b>件数制限あり。入力の先頭部分だけの結果です。</b></p>')
    parts.extend('<p>'+html.escape(line)+'</p>' for line in lines)
    parts.append('<p>同一トリップは観測日・DAILYID・TRIP_RK・始終点時刻で判定します。異なるトリップID間の乗り換えは対象外です。'
                 'コード混在には不明・欠損も含めます。複数の具体的な手段はコード1〜8が2種類以上あるものです。'
                 '混在は推定判定の不整合でも発生し、実際の乗り換えの証明にはなりません。'
                 '距離は球面上の始終点間直線距離で、実移動距離や手段別の距離ではありません。'
                 '所要時間はトリップ全体の始終点時刻差で、手段別の時間ではありません。'
                 '始終点座標が複数ある場合は最初の記録を使用し、欠損・不正座標や不正な時間は平均から除外します。</p>')
    parts.append('<h2>交通手段の組み合わせ</h2><table><tr><th>組み合わせ</th><th>トリップ数</th></tr>')
    parts.extend(f'<tr><td>{html.escape(k)}</td><td>{v:,}</td></tr>' for k,v in combinations.most_common())
    parts.append('</table><h2>該当トリップ（先頭30件）</h2><p>全件はmixed_trips.csvを参照。該当0件の場合は表は空です。</p><table><tr>')
    parts.extend(f'<th>{x}</th>' for x in ['観測日','始点時刻','終点時刻','手段','複数の具体的手段','ポイント数','直線距離km','所要時間分'])
    parts.append('</tr>')
    for row in examples:
        parts.append('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in row)+'</tr>')
    parts.append('</table></html>')
    (out/'report.html').write_text(''.join(parts),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?',type=Path,default=Path.home()/'Downloads'/'260320_cellphone_trip_data.zip')
    parser.add_argument('--output',type=Path,default=Path('mixed_trip_modes_output'))
    parser.add_argument('--limit',type=int,default=0,help='先頭Nポイント。0は全件')
    args=parser.parse_args()
    if args.limit<0:
        parser.error('--limit は0以上を指定してください')
    try:
        out=args.output.resolve()
        out.mkdir(parents=True,exist_ok=True)
        path=out/'mixed.sqlite3'
        if path.exists():
            raise ValueError('出力先のDBが既にあります。別の--outputを指定してください。')
        with sqlite3.connect(path) as db:
            stats=scan(args,db)
            report(db,out,stats,args.limit)
        print(f'完了: {out / "report.html"}')
        return 0
    except (OSError,ValueError,sqlite3.Error,zipfile.BadZipFile,EOFError,csv.Error) as exc:
        print(f'エラー: {exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
