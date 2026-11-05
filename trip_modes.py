"""完了済みの分析DBから交通手段別トリップ数を表示する。Python 3.10以上。"""
import argparse
import csv
import html
import json
import sqlite3
import sys
from pathlib import Path


MODES = {
    '1': '航空機', '2': '鉄道（新幹線）', '3': '鉄道（その他）',
    '4': '自動車（高速道路）', '5': '自動車（その他）', '6': '船舶（海上）',
    '7': '自転車', '8': '徒歩', '9': '不明',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=Path('full_output'),
                        help='data.pyの完了済み出力フォルダ（既定: full_output）')
    parser.add_argument('--output', type=Path, default=Path('trip_modes_output'),
                        help='今回の表・レポートの保存先')
    args = parser.parse_args()
    try:
        source = args.input_dir.resolve()
        dbpath = source / 'analysis.sqlite3'
        if not dbpath.is_file() or not (source / 'summary.json').is_file():
            raise ValueError('完了済みのanalysis.sqlite3とsummary.jsonが必要です。data.pyの完了を確認してください。')
        summary = json.loads((source / 'summary.json').read_text(encoding='utf-8'))
        # 元DBは読み取り専用で開く。位置情報ZIPの再読み込みは不要。
        with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True) as db:
            counts = dict(db.execute('SELECT mode, COUNT(*) FROM trips GROUP BY mode'))
            first, last = db.execute('SELECT MIN(day), MAX(day) FROM points').fetchone()
        total = sum(counts.values())
        if total != summary['trips']:
            raise ValueError('DBとsummary.jsonのトリップ数が一致しません。分析の完了状況を確認してください。')
        # 未観測の手段も0件で表示。欠損・未定義コードも分母に含める。
        rows = [(code, MODES.get(code, '欠損' if code in ('', 'missing', None) else f'未定義（{code}）'),
                 counts.get(code, 0), counts.get(code, 0) / total * 100 if total else 0)
                for code in set(MODES) | set(counts)]
        rows.sort(key=lambda r: (-r[2], str(r[0])))
        print(f'交通手段別トリップ数\n観測期間: {first or "なし"} ～ {last or "なし"}')
        print(f'全トリップ数: {total:,} 件\n')
        for _, label, count, share in rows:
            print(f'{label}: {count:,} 件（{share:.2f}%）')
        if summary.get('sample_limit'):
            print('\n注意: 件数制限あり。先頭部分だけの集計です。')
        out = args.output.resolve()
        out.mkdir(parents=True, exist_ok=True)
        with (out / 'trip_modes.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['交通手段コード', '交通手段', 'トリップ数', '割合（%）'])
            writer.writerows((code, label, count, f'{share:.4f}') for code, label, count, share in rows)
        parts = ['<!doctype html><html lang="ja"><meta charset="utf-8">',
                 '<title>交通手段別トリップ数</title><style>',
                 'body{font-family:Meiryo,sans-serif;max-width:900px;margin:40px auto;padding:20px;color:#193047;background:#f5f7fa}',
                 'table{border-collapse:collapse;width:100%;background:white}th,td{padding:12px;border-bottom:1px solid #ddd;text-align:left}',
                 '.bar{height:14px;background:#168c9c;min-width:0}td:last-child{width:30%}</style>',
                 '<h1>交通手段別トリップ数</h1>',
                 f'<p>観測期間：{first or "なし"} ～ {last or "なし"}<br>全トリップ数：<b>{total:,} 件</b></p>',
                 '<p>分析対象は、携帯電話の位置情報に付与された推定交通手段別の移動回数です。',
                 '割合の分母は不明・欠損を含む全トリップ数です。棒の長さも同じ割合を表します。</p>']
        if summary.get('sample_limit'):
            parts.append('<p><b>件数制限あり：先頭部分だけの集計です。全体の傾向には一般化できません。</b></p>')
        parts.append('<table><tr><th>交通手段</th><th>トリップ数</th><th>割合</th><th>全体に占める割合</th></tr>')
        for _, label, count, share in rows:
            parts.append(f'<tr><td>{html.escape(label)}</td><td>{count:,}</td><td>{share:.2f}%</td>'
                         f'<td><div class="bar" style="width:{share:.4f}%"></div></td></tr>')
        parts.append(f'<tr><th>合計</th><th>{total:,}</th><th>{100 if total else 0:.2f}%</th><td></td></tr></table>')
        parts.append('<p>トリップは観測日・DAILYID・TRIP_RK・始終点時刻で重複除去した件数です。'
                     '人数や地域全体の交通量ではありません。日をまたぐ同一移動が別計上される場合があります。'
                     '表示割合は丸めているため合計が100%と一致しない場合があります。</p></html>')
        (out / 'report.html').write_text(''.join(parts), encoding='utf-8')
        print(f'\nレポート: {out / "report.html"}\nCSV: {out / "trip_modes.csv"}')
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
