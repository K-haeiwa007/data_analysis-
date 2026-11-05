"""完了済みの分析DBから観測日別の移動件数（トリップ数）を表示する。"""
import argparse
import csv
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=Path('full_output'),
                        help='data.pyの完了済み出力フォルダ')
    parser.add_argument('--output', type=Path, default=Path('daily_trips_output'),
                        help='日別集計の保存先')
    args = parser.parse_args()
    try:
        source = args.input_dir.resolve()
        dbpath = source / 'analysis.sqlite3'
        if not dbpath.is_file() or not (source / 'summary.json').is_file():
            raise ValueError('完了済みのanalysis.sqlite3とsummary.jsonが必要です。')
        summary = json.loads((source / 'summary.json').read_text(encoding='utf-8'))
        with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True) as db:
            counts = dict(db.execute('SELECT day,COUNT(*) FROM trips GROUP BY day'))
            # 位置記録はあるが有効なトリップがない日も0件で表示する。
            days = sorted({r[0] for r in db.execute('SELECT DISTINCT day FROM points')} | set(counts))
        total = sum(counts.values())
        if total != summary['trips']:
            raise ValueError('DBとsummary.jsonのトリップ数が一致しません。分析の完了状況を確認してください。')
        rows = [(day, '月火水木金土日'[date.fromisoformat(day).weekday()], counts.get(day, 0),
                 counts.get(day, 0) / total * 100 if total else 0) for day in days]
        print('日付別の移動件数（観測日別トリップ数）')
        print(f'対象: {len(days)}日 / 合計: {total:,} 件\n')
        for day, weekday, count, share in rows:
            print(f'{day}（{weekday}）: {count:,} 件（{share:.2f}%）')
        sample_note = '件数制限あり：先頭部分だけの集計です。全体の傾向には一般化できません。' if summary.get('sample_limit') else ''
        if sample_note:
            print(f'\n注意: {sample_note}')
        out = args.output.resolve()
        out.mkdir(parents=True, exist_ok=True)
        with (out / 'daily_trips.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['観測日', '曜日', '移動件数（トリップ数）', '全期間に占める割合（%）'])
            writer.writerows((day, weekday, count, f'{share:.4f}') for day, weekday, count, share in rows)
        maximum = max((r[2] for r in rows), default=0)
        parts = ['<!doctype html><html lang="ja"><meta charset="utf-8">',
                 '<title>日付別の移動件数</title><style>',
                 'body{font-family:Meiryo,sans-serif;max-width:1000px;margin:40px auto;padding:20px;color:#193047;background:#f5f7fa}',
                 'table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;border-bottom:1px solid #ddd;text-align:left}',
                 '.bar{height:15px;background:#168c9c}.weekend{background:#edf4ff}td:last-child{width:35%}</style>',
                 '<h1>日付別の移動件数</h1>',
                 f'<p>対象：{days[0] if days else "なし"} ～ {days[-1] if days else "なし"}（観測のある{len(days)}日）<br>合計：<b>{total:,}トリップ</b></p>',
                 '<p>携帯電話の位置情報に付与された移動を、観測日別に集計しています。'
                 '割合の分母は全期間のトリップ数、棒の長さは最多の日を100%とした相対値です。土日は薄青色で表示しています。祝日は判別していません。</p>']
        if sample_note:
            parts.append(f'<p><b>{sample_note}</b></p>')
        if rows:
            peak_days = '、'.join(r[0] for r in rows if r[2] == maximum)
            parts.append(f'<p>観測のある日の日平均：<b>{total/len(rows):,.1f}件</b>。最多：{peak_days}（{maximum:,}件）。</p>')
        else:
            parts.append('<p>対象データがありません。</p>')
        parts.append('<table><tr><th>観測日</th><th>曜日</th><th>移動件数</th><th>割合</th><th>日別比較</th></tr>')
        for day, weekday, count, share in rows:
            css = ' class="weekend"' if weekday in '土日' else ''
            width = count / maximum * 100 if maximum else 0
            parts.append(f'<tr{css}><td>{day}</td><td>{weekday}</td><td>{count:,}</td><td>{share:.2f}%</td>'
                         f'<td><div class="bar" style="width:{width:.4f}%"></div></td></tr>')
        parts.append(f'<tr><th>合計</th><td></td><th>{total:,}</th><th>{100 if total else 0:.2f}%</th><td></td></tr></table>')
        parts.append('<p>同じ移動は観測日・DAILYID・TRIP_RK・始終点時刻で重複除去しています。'
                     '日付は観測日のため、移動開始日別の集計とは異なる場合があります。日をまたぐ同一移動が別計上される場合があります。'
                     '記録がない日は表示せず、位置記録があってトリップがない日は0件で表示します。'
                     '人数や地域全体の交通量ではありません。日ごとの取得対象者数の違いも件数に影響します。'
                     '表示割合は丸めのため合計が100%にならない場合があります。</p></html>')
        (out / 'report.html').write_text(''.join(parts), encoding='utf-8')
        print(f'\nレポート: {out / "report.html"}\nCSV: {out / "daily_trips.csv"}')
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
