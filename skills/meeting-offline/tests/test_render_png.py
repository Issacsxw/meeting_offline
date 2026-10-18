import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import minutes_html
import render_png
import workspace as w
import test_workspace as fixtures


def browser():
    try:
        return render_png.find_browser()
    except render_png.BrowserNotFound:
        return None


DOCUMENT = {'topic': '测试纪要', 'meeting_time': '', 'participants': '甲、乙', 'summary': '概要。',
            'points': [{'title': '议题', 'text': '结论。'}],
            'todos': [{'task': '提供报价', 'owner': '甲', 'deadline': '—'}]}
def png_pixel(data, x, y):
    """RGB at (x, y) of an 8-bit RGBA PNG, decoded with the standard library."""
    import struct, zlib
    position, idat = 8, b''
    while position < len(data):
        length, kind = struct.unpack('>I4s', data[position:position + 8])
        chunk = data[position + 8:position + 8 + length]
        if kind == b'IHDR':
            width, height, depth, colour = struct.unpack('>IIBB', chunk[:10])
            assert (depth, colour) == (8, 6), 'expected 8-bit RGBA'
        elif kind == b'IDAT':
            idat += chunk
        position += 12 + length
    raw, stride, previous = zlib.decompress(idat), width * 4, bytearray(width * 4)
    for row in range(y + 1):
        kind, line = raw[row * (stride + 1)], bytearray(raw[row * (stride + 1) + 1:(row + 1) * (stride + 1)])
        for i in range(stride):
            left, up = line[i - 4] if i >= 4 else 0, previous[i]
            corner = previous[i - 4] if i >= 4 else 0
            if kind == 1: line[i] = (line[i] + left) & 255
            elif kind == 2: line[i] = (line[i] + up) & 255
            elif kind == 3: line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                p = left + up - corner; pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        previous = line
    return tuple(previous[x * 4:x * 4 + 3])


PNG = render_png.PNG_SIGNATURE + b'\0\0\0\rIHDR' + (920).to_bytes(4, 'big') + (400).to_bytes(4, 'big')


class OfflineTests(unittest.TestCase):
    def test_hook_runs_page_export_before_body_end(self):
        page = render_png.inject(minutes_html.render(DOCUMENT))
        self.assertIn('makePng(collect())', page)
        self.assertLess(page.index("out.id='headless-png'"),
                        page.lower().rindex('</body>'))
        with self.assertRaises(ValueError):
            render_png.inject('<html><body>其他页面</body></html>')

    def test_extract_validates_png_and_reports_page_errors(self):
        url = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        data = render_png.extract('<pre id="headless-png" data-state="ok">' + url + '</pre>')
        self.assertEqual(render_png.size(data), (920, 400))
        with self.assertRaisesRegex(ValueError, '内容较长'):
            render_png.extract('<pre id="headless-png" data-state="error">内容较长，请精简</pre>')
        with self.assertRaises(RuntimeError):
            render_png.extract('<pre id="headless-png" data-state="ok">data:image/png;base64,' +
                               base64.b64encode(b'not png').decode() + '</pre>')
        with self.assertRaises(RuntimeError):
            render_png.extract('<html></html>')

    def test_missing_browser_keeps_html_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'MEETING_OFFLINE_BROWSER': str(Path(tmp) / 'none')}):
            html = Path(tmp) / '纪要.html'; html.write_text(minutes_html.render(DOCUMENT), encoding='utf-8')
            result = minutes_html.png_for(html)
            self.assertIsNone(result['png'])
            self.assertIn('浏览器', result['png_error'])


class WorkspacePublishTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    tearDown = fixtures.WorkspaceTests.tearDown

    def analysis(self):
        return {'sources': {'4': {'title': '测试会议', 'meeting_date': None,
            'actions': [{'task': '提供报价', 'owner': '4-A', 'owner_basis': 'self_commitment', 'evidence': [0]}],
            'reader': {'topic': '简明会议', 'summary': '概要。', 'summary_evidence': [0],
                       'points': [{'title': '观点', 'text': '提供报价。', 'evidence': [0], 'attribution': ['4-A'], 'status': 'direction'}],
                       'todos': [{'action_index': 0, 'task': '交付报价单'}]}}}}

    def test_without_browser_export_succeeds_and_publish_accepts_manual_png(self):
        with mock.patch.dict(os.environ, {'MEETING_OFFLINE_BROWSER': str(self.base / 'none')}):
            w.set_analysis(self.root, self.analysis(), 1)
        files = w.read(self.root / 'exports/latest.json')['files']['4']
        self.assertNotIn('png', files); self.assertIn('png_error', files)
        with self.assertRaisesRegex(ValueError, '没有 PNG'):
            w.publish(self.root)
        manual = self.base / 'manual.png'; manual.write_bytes(PNG)
        publication = w.publish(self.root, ['4'], {'4': str(manual)})
        self.assertEqual(publication['files']['4']['png']['sha256'], w.file_digest(manual))

    @unittest.skipUnless(browser(), 'no local Chrome/Edge/Chromium')
    def test_export_renders_png_and_publish_records_checksums(self):
        w.set_analysis(self.root, self.analysis(), 1)
        latest = w.read(self.root / 'exports/latest.json'); files = latest['files']['4']
        png = Path(files['png'])
        self.assertTrue(png.read_bytes().startswith(render_png.PNG_SIGNATURE))
        self.assertEqual(render_png.size(png.read_bytes())[0], 1840)
        publication = w.publish(self.root)
        self.assertEqual(publication['export_version'], latest['version'])
        self.assertEqual(publication['files']['4']['png']['sha256'], w.file_digest(png))
        self.assertEqual(publication['files']['4']['html']['sha256'], w.file_digest(files['html']))


@unittest.skipUnless(browser(), 'no local Chrome/Edge/Chromium')
class BrowserTests(unittest.TestCase):
    def test_edited_html_exports_new_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / '纪要.html'; original.write_text(minutes_html.render(DOCUMENT), encoding='utf-8')
            first = render_png.render(original)
            self.assertEqual(Path(first['png']), original.with_suffix('.png'))
            edited_doc = {**DOCUMENT, 'points': DOCUMENT['points'] * 4, 'todos': DOCUMENT['todos'] + [
                {'task': '新增事项', 'owner': '乙', 'deadline': '周五', 'done': True}]}
            edited = Path(tmp) / '纪要-修改版.html'; edited.write_text(minutes_html.render(edited_doc), encoding='utf-8')
            second = render_png.render(edited, Path(tmp) / 'out' / '新图.png')
            self.assertEqual(first['width'], second['width'])
            self.assertGreater(second['height'], first['height'])

    def test_png_follows_the_chosen_theme(self):
        expected = {'warm': (250, 249, 245), 'business': (255, 255, 255), 'dark': (31, 30, 28)}
        with tempfile.TemporaryDirectory() as tmp:
            for theme, paper in expected.items():
                html = Path(tmp) / (theme + '.html'); html.write_text(minutes_html.render({**DOCUMENT, 'theme': theme}), encoding='utf-8')
                data = Path(render_png.render(html)['png']).read_bytes()
                self.assertEqual(png_pixel(data, 40, 400), paper, theme)

    def test_every_layout_exports_an_image(self):
        document = {**DOCUMENT, 'points': DOCUMENT['points'] * 3, 'todos': DOCUMENT['todos'] + [
            {'task': '已完成事项', 'owner': '—', 'deadline': '—', 'done': True}]}
        with tempfile.TemporaryDirectory() as tmp:
            for layout in minutes_html.LAYOUTS:
                html = Path(tmp) / (layout + '.html')
                html.write_text(minutes_html.render({**document, 'layout': layout, 'theme': 'dark'}), encoding='utf-8')
                data = Path(render_png.render(html)['png']).read_bytes()
                width, height = render_png.size(data)
                self.assertEqual(width, 1440 if layout == 'checklist' else 1840, layout)
                self.assertEqual(png_pixel(data, 4, height - 10), (31, 30, 28), layout)  # page background follows the theme

    def test_page_error_is_reported(self):
        long_doc = {**DOCUMENT, 'points': [{'title': '议题', 'text': '很长的内容。' * 400}] * 12}
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / '纪要.html'; html.write_text(minutes_html.render(long_doc), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '内容较长'):
                render_png.render(html)
            self.assertFalse(html.with_suffix('.png').exists())
            self.assertIn('内容较长', minutes_html.png_for(html)['png_error'])


if __name__ == '__main__':
    unittest.main()
