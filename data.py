"""Agoop交通判定付きポイントデータの分析 (Python 3.10+, 外部依存なし)。"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import math
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

MODES = dict(enumerate(['航空機', '鉄道（新幹線）', '鉄道（その他）',
                       '自動車（高速道路）', '自動車（その他）', '船舶（海上）',
                       '自転車', '徒歩', '不明'], 1))
REQUIRED = {'DAILYID', 'YEAR', 'MONTH', 'DAY', 'HOUR', 'LATITUDE', 'LONGITUDE',
            'SPEED', 'TRAFFIC_MODE', 'TRIP_RK', 'LOGTIME_START', 'LOGTIME_END',
            'MESH_START', 'MESH_END'}


def value(s):
    return '' if s is None or s.strip().lower() in ('', 'null', 'nan') else s.strip()


def number(s):
    try:
        n = float(value(s))
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def iso_date(s):
    return date.fromisoformat(s).isoformat()


def duration(start, end):
    try:
        n = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
        return n if n >= 0 else None
    except (ValueError, TypeError):
        return None


def export(db, out, name, query):
    cur = db.execute(query)
    with (out / f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in cur.description])
        w.writerows(cur)


def analyze(args):
    if args.start and args.end and args.start > args.end:
        raise ValueError('--start は --end 以前を指定してください')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # 新規専用DB。既存結果を誤って二重集計しない。
    dbpath = out / 'analysis.sqlite3'
    if dbpath.exists():
        raise ValueError(f'{dbpath} は既にあります。別の --output を指定してください')
    db = sqlite3.connect(dbpath)
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE users(day TEXT, id TEXT, PRIMARY KEY(day,id)) WITHOUT ROWID;
      CREATE TABLE trips(day TEXT, id TEXT, rk TEXT, start TEXT, end TEXT,
        mode TEXT, origin TEXT, destination TEXT, minutes REAL,
        PRIMARY KEY(day,id,rk,start,end)) WITHOUT ROWID;
      CREATE TABLE points(day TEXT, hour INTEGER, mode TEXT, city TEXT,
        count INTEGER, speed_n INTEGER, speed_sum REAL);
    ''')
    stats = Counter()
    missing = Counter()
    groups = {}
    users, trips = [], []
    sources = []
    columns = set()

    def flush():
        db.executemany('INSERT OR IGNORE INTO users VALUES (?,?)', users)
        db.executemany('INSERT OR IGNORE INTO trips VALUES (?,?,?,?,?,?,?,?,?)', trips)
        db.commit()
        users.clear()
        trips.clear()

    try:
        with zipfile.ZipFile(args.input) as archive:
            entries = sorted((e for e in archive.infolist() if e.filename.lower().endswith('.csv.gz')),
                             key=lambda e: e.filename)
            if not entries:
                raise ValueError('ZIP内に .csv.gz がありません')
            stop = False
            for entry in entries:
                # ファイルパスの日付で事前に絞り、各レコードの日付も検証する。
                folder = entry.filename.split('/')[-2]
                if len(folder) == 8 and folder.isdigit():
                    fileday = f'{folder[:4]}-{folder[4:6]}-{folder[6:]}'
                    if (args.start and fileday < args.start) or (args.end and fileday > args.end):
                        continue
                sources.append(entry.filename)
                print(f'読み込み: {entry.filename}', flush=True)
                with archive.open(entry) as raw, gzip.GzipFile(fileobj=raw) as gz:
                    with io.TextIOWrapper(gz, encoding='utf-8-sig', newline='') as text:
                        reader = csv.DictReader(text)
                        absent = REQUIRED - set(reader.fieldnames or [])
                        if absent:
                            raise ValueError(f'必須列がありません: {sorted(absent)}')
                        columns.update(reader.fieldnames)
                        for row in reader:
                            stats['rows_read'] += 1
                            if None in row or any(row[c] is None for c in REQUIRED):
                                stats['malformed_rows'] += 1
                                continue
                            try:
                                day = date(int(row['YEAR']), int(row['MONTH']), int(row['DAY'])).isoformat()
                                hour = int(row['HOUR'])
                                if not 0 <= hour <= 23:
                                    raise ValueError()
                            except ValueError:
                                stats['invalid_datetime_rows'] += 1
                                continue
                            if (args.start and day < args.start) or (args.end and day > args.end):
                                continue
                            stats['points'] += 1
                            for c, v in row.items():
                                if not value(v):
                                    missing[c] += 1
                            lat, lon = number(row['LATITUDE']), number(row['LONGITUDE'])
                            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                                stats['invalid_coordinates'] += 1
                            mode = value(row['TRAFFIC_MODE']) or 'missing'
                            city = value(row.get('CITYCODE')) or 'missing'
                            key = day, hour, mode, city
                            g = groups.setdefault(key, [0, 0, 0.0])
                            g[0] += 1
                            speed = number(row['SPEED'])
                            if speed is not None and speed >= 0:
                                g[1] += 1
                                g[2] += speed * 3.6
                            elif value(row['SPEED']):
                                stats['invalid_speed'] += 1
                            uid = value(row['DAILYID'])
                            if uid:
                                users.append((day, uid))
                            rk, start, end = (value(row[c]) for c in ('TRIP_RK', 'LOGTIME_START', 'LOGTIME_END'))
                            if uid and rk and start and end:
                                trips.append((day, uid, rk, start, end, mode,
                                              value(row['MESH_START']), value(row['MESH_END']), duration(start, end)))
                            else:
                                stats['points_without_trip_key'] += 1
                            if stats['points'] % 10000 == 0:
                                flush()
                            if stats['points'] % 500000 == 0:
                                print(f"  {stats['points']:,} ポイント処理済み", flush=True)
                            if args.limit and stats['points'] >= args.limit:
                                stop = True
                                break
                if stop:
                    break
        flush()
        db.executemany('INSERT INTO points VALUES (?,?,?,?,?,?,?)',
                       [(*k, *v) for k, v in groups.items()])
        db.commit()
        queries = report_queries()
        for name, query in queries.items():
            export(db, out, name, query)
        with (out / 'missing.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['column', 'missing_points', 'missing_rate'])
            for c in sorted(columns):
                w.writerow([c, missing[c], missing[c] / stats['points'] if stats['points'] else 0])
        summary = dict(stats)
        summary.update(trips=db.execute('SELECT COUNT(*) FROM trips').fetchone()[0],
                       invalid_duration_trips=db.execute('SELECT COUNT(*) FROM trips WHERE minutes IS NULL').fetchone()[0],
                       input=str(args.input.resolve()), sources=sources, sample_limit=args.limit,
                       start=args.start, end=args.end, traffic_modes=MODES)
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        render_report(db, out, summary, queries)
        print(f"完了: {stats['points']:,} ポイント / {summary['trips']:,} トリップ\nレポート: {out / 'report.html'}")
    finally:
        db.close()


def report_queries():
    return {
            'daily': '''SELECT day, SUM(count) points,
                (SELECT COUNT(*) FROM users u WHERE u.day=p.day) daily_ids,
                (SELECT COUNT(*) FROM trips t WHERE t.day=p.day) trips
                FROM points p GROUP BY day ORDER BY day''',
            'hourly': 'SELECT hour, SUM(count) points FROM points GROUP BY hour ORDER BY hour',
            'traffic_points': '''SELECT mode, SUM(count) points,
                SUM(speed_sum)/NULLIF(SUM(speed_n),0) mean_speed_kmh,
                SUM(speed_n) valid_speed_points FROM points GROUP BY mode ORDER BY points DESC''',
            'cities': 'SELECT city, SUM(count) points FROM points GROUP BY city ORDER BY points DESC',
            'traffic_trips': '''SELECT mode, COUNT(*) trips, AVG(minutes) mean_duration_minutes,
                COUNT(minutes) valid_duration_trips FROM trips GROUP BY mode ORDER BY trips DESC''',
            'od_mesh': '''SELECT origin, destination, COUNT(*) trips FROM trips
                WHERE origin<>'' AND destination<>'' GROUP BY origin,destination ORDER BY trips DESC''',
        }


def interpretation(db, summary):
    """数値で確認できる事実と、検証が必要な仮説を分離する。"""
    findings = []
    peak = db.execute('SELECT hour,SUM(count) n FROM points GROUP BY hour ORDER BY n DESC,hour LIMIT 1').fetchone()
    total = summary.get('points', 0)
    if peak and total:
        findings.append((f'位置記録が最も多いのは{peak[0]}時台で、{peak[1]:,}件（全ポイントの{peak[1]/total:.1%}）です。',
                         'この時間帯に移動・滞在の観測が集中している可能性があります。記録頻度の差も影響します。',
                         '平日と休日に分け、トリップ開始時刻別の件数と比べます。通勤・通学の判定には移動目的等の情報も必要です。'))
    modes = db.execute('SELECT mode,COUNT(*) n FROM trips GROUP BY mode ORDER BY n DESC,mode').fetchall()
    if modes and summary['trips']:
        mode, n = modes[0]
        label = MODES.get(int(mode), mode) if str(mode).isdigit() else mode
        unknown = sum(count for code, count in modes if code not in {str(k) for k in range(1, 9)})
        findings.append((f'最多の推定交通手段は「{label}」で{n:,}トリップ（全トリップの{n/summary["trips"]:.1%}）。不明・欠損・未定義コードは計{unknown:,}トリップです。',
                         'このデータの取得対象では、その手段が多く観測されていると考えられます。地域全体の交通分担率は直接求められません。',
                         '地域・曜日別に比率を比較し、既存の交通調査と取得対象の偏りを確認します。'))
    days = db.execute(report_queries()['daily']).fetchall()
    weekday = [r for r in days if date.fromisoformat(r[0]).weekday() < 5]
    weekend = [r for r in days if date.fromisoformat(r[0]).weekday() >= 5]
    if weekday and weekend and not summary.get('sample_limit'):
        a = sum(r[3] for r in weekday) / len(weekday)
        b = sum(r[3] for r in weekend) / len(weekend)
        norm_a = sum(r[3] for r in weekday) / sum(r[2] for r in weekday) if sum(r[2] for r in weekday) else None
        norm_b = sum(r[3] for r in weekend) / sum(r[2] for r in weekend) if sum(r[2] for r in weekend) else None
        normalized = f' 延べ日別ID当たりでは月〜金{norm_a:.2f}回、土日{norm_b:.2f}回です。' if norm_a is not None and norm_b is not None else ''
        findings.append((f'月〜金{len(weekday)}日の日平均は{a:,.1f}トリップ、土日{len(weekend)}日の日平均は{b:,.1f}トリップです。'+normalized,
                         '曜日区分によって観測された移動回数が異なる可能性があります。対象者数やデータ取得状況の違いも考えられます。',
                         '日別ID数と一緒に比較し、祝日・欠測・天候・イベントを別途確認します。この分類は曜日だけで、祝日は判別していません。'))
    city = db.execute("SELECT city,SUM(count) n FROM points WHERE city<>'missing' GROUP BY city ORDER BY n DESC,city LIMIT 1").fetchone()
    if city and total:
        findings.append((f'観測地点コード「{city[0]}」が最多で{city[1]:,}ポイント（全ポイントの{city[1]/total:.1%}）です。',
                         '特定の地域に観測が集中している可能性があります。滞在時間や取得対象の偏りでも記録数は増えます。',
                         'コードの桁数と地域対応を確認し、地域別ユニークID数や面積当たりの値を比較します。地名は未照合です。'))
    parts = ['<h2>結果から読み取れること・考察</h2>',
             '<p>以下の「事実」は今回の集計値、「仮説」は追加検証が必要な解釈です。因果関係や移動目的を確定するものではありません。</p>']
    if summary.get('sample_limit'):
        parts.append('<p><b>先頭部分だけの集計なので、全期間・地域の傾向へ一般化できません。</b></p>')
    if not findings:
        parts.append('<p>対象データがないため傾向を算出できません。</p>')
    for fact, hypothesis, next_step in findings:
        parts.append('<article><p><b>確認できた事実：</b>'+html.escape(fact)+'</p><p><b>考えられる仮説：</b>'+html.escape(hypothesis)+'</p><p><b>確かめるには：</b>'+html.escape(next_step)+'</p></article>')
    parts.append('<p>OD上位は繰り返し観測される出発地・到着地の候補です。メッシュの地図照合と方向・時間帯別の分析により移動のつながりを調べられます。現状では施設名や通勤・観光等の目的は特定していません。</p>')
    return parts


def render_report(db, out, summary, queries):
    first, last, days = db.execute('SELECT MIN(day),MAX(day),COUNT(DISTINCT day) FROM points').fetchone()
    parts = ['<!doctype html><meta charset="utf-8"><title>位置情報分析</title>',
             '<style>body{font-family:Meiryo,sans-serif;max-width:1100px;margin:40px auto;padding:20px;background:#f5f7fa;color:#193047}table{border-collapse:collapse;background:white;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}.bar{height:12px;background:#168c9c}h2{margin-top:40px}</style>',
             '<h1>交通判定付き位置情報データ分析</h1>',
             f"<p>ポイント数: {summary.get('points', 0):,} ／ トリップ数: {summary['trips']:,}</p>",
             '<p>トリップは観測日・DAILYID・TRIP_RK・始終点時刻で重複除去。日をまたぐ同一移動は別計上となる場合があります。',
             '人数は日別ID数であり、延べ観測・推定交通手段の集計です。母集団の交通量や実移動距離を表しません。</p>']
    parts.extend([
        '<h2>何について分析しているか</h2>',
        '<p>携帯電話の位置記録と、それに付与された推定交通手段・トリップ情報を使い、<b>移動の時間的な偏り、観測地域の偏り、交通手段の構成、出発地と到着地のつながり</b>を調べています。</p>',
        f'<p>入力：{html.escape(str(summary["input"]))}<br>実際に観測された期間：{first or "なし"} 〜 {last or "なし"}（{days}日）<br>読み込み対象ファイル数：{len(summary["sources"])}。対象地域は入力データに含まれる観測地点で、特定地域への追加絞り込みはしていません。</p>',
        '<p><b>ポイント：</b>位置を記録した回数。<b>日別ID：</b>その日に観測された識別子の数。<b>トリップ：</b>始終点が付与された移動のまとまり。これらは異なる単位です。</p>',
    ])
    parts.extend(interpretation(db, summary))
    if summary['sample_limit']:
        parts.append('<p><b>件数制限あり：ファイル先頭からの部分集計です。無作為標本ではありません。</b></p>')
    titles = {'daily': '日別ポイント・日別ID・トリップ数', 'hourly': '時間帯別ポイント数',
              'traffic_points': '交通手段別ポイント数・平均速度（km/h）', 'cities': '市区町村コード別ポイント数',
              'traffic_trips': '交通手段別トリップ数・平均所要時間（分）', 'od_mesh': '始終点125mメッシュ別トリップ数'}
    descriptions = {
        'daily': '問い：日によって観測規模や移動回数は違うか。ポイント数だけでなく日別ID数も確認します。',
        'hourly': '問い：何時に位置記録が集中するか。観測時刻の集計で、出発時刻の集計ではありません。',
        'traffic_points': '問い：各交通手段でどれだけ位置記録が得られ、どの程度の速度が観測されたか。平均速度は有効なポイントの算術平均です。',
        'cities': '問い：どの地域コードに観測が集中するか。記録数は地域人口や来訪人数とは異なります。',
        'traffic_trips': '問い：推定交通手段ごとに何回移動し、所要時間はどの程度か。不正な所要時間は平均から除外しています。',
        'od_mesh': '問い：どの出発地と到着地の組み合わせが多いか。経由経路や移動目的はこの表だけでは分かりません。',
    }
    labels = {'day': '観測日', 'points': 'ポイント数', 'daily_ids': '日別ID数', 'trips': 'トリップ数',
              'hour': '観測時刻（時台）', 'mode': '推定交通手段', 'city': '観測地点の地域コード',
              'mean_speed_kmh': '平均速度（km/h）', 'valid_speed_points': '速度が有効なポイント数',
              'mean_duration_minutes': '平均所要時間（分）', 'valid_duration_trips': '所要時間が有効なトリップ数',
              'origin': '出発125mメッシュ', 'destination': '到着125mメッシュ'}
    for name, query in queries.items():
        cur = db.execute(query)
        rows = cur.fetchmany(30)
        parts.append(f'<h2>{titles[name]}</h2><p>{descriptions[name]}</p><p>最大30行を表示。全件は {name}.csv を参照。</p><table><tr>')
        parts.extend(f'<th>{html.escape(labels.get(d[0], d[0]))}</th>' for d in cur.description)
        parts.append('</tr>')
        maximum = max((r[1] for r in rows), default=1) if name in ('hourly', 'cities') else 0
        for row in rows:
            parts.append('<tr>')
            for i, v in enumerate(row):
                if i == 0 and name.startswith('traffic'):
                    v = MODES.get(int(v), v) if str(v).isdigit() else v
                display = '' if v is None else f'{v:.2f}' if isinstance(v, float) else str(v)
                parts.append(f'<td>{html.escape(display)}</td>')
            parts.append('</tr>')
            if maximum:
                parts.append(f'<tr><td colspan="2"><div class="bar" style="width:{row[1]/maximum*100:.2f}%"></div></td></tr>')
        parts.append('</table>')
    (out / 'report.html').write_text(''.join(parts), encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', nargs='?', type=Path,
                   default=Path.home() / 'Downloads' / '260320_cellphone_trip_data.zip')
    p.add_argument('--output', type=Path, default=Path('analysis_output'))
    p.add_argument('--start', type=iso_date, help='開始日 YYYY-MM-DD（含む）')
    p.add_argument('--end', type=iso_date, help='終了日 YYYY-MM-DD（含む）')
    p.add_argument('--limit', type=int, default=0, help='分析ポイント数上限。0は全件')
    p.add_argument('--report-only', action='store_true', help='完了済み出力のHTMLのみ再生成（ZIP再集計なし）')
    args = p.parse_args()
    if args.limit < 0:
        p.error('--limit は0以上を指定してください')
    try:
        if args.report_only:
            out = args.output.resolve()
            if not (out / 'summary.json').is_file() or not (out / 'analysis.sqlite3').is_file():
                raise ValueError('完了済みのsummary.jsonとanalysis.sqlite3が必要です。分析処理の完了を待ってください。')
            summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
            with sqlite3.connect((out / 'analysis.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                render_report(db, out, summary, report_queries())
            print(f'レポート更新: {out / "report.html"}')
        else:
            analyze(args)
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error, EOFError, csv.Error) as e:
        print(f'エラー: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
