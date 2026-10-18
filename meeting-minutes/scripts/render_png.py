#!/usr/bin/env python3
"""Render the minutes PNG from an editable HTML with a local headless browser. No network."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
# Calls the page's own makePng(collect()) so the file matches the in-page "导出图片" button,
# including any edits saved into the HTML.
HOOK = """<script>
(async()=>{const out=document.createElement('pre');out.id='headless-png';
try{const png=await makePng(collect());out.dataset.state='ok';out.textContent=png.url}
catch(error){out.dataset.state='error';out.textContent=String(error&&error.message||error)}
document.body.append(out)})();
</script>"""
CANDIDATES = {
    'darwin': ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
               '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
               '/Applications/Chromium.app/Contents/MacOS/Chromium',
               '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'],
    'win32': [r'%ProgramFiles%\Google\Chrome\Application\chrome.exe',
              r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe',
              r'%LocalAppData%\Google\Chrome\Application\chrome.exe',
              r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe',
              r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'],
}
COMMANDS = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge', 'chrome']


class BrowserNotFound(RuntimeError):
    pass


def find_browser(explicit=None):
    if explicit or os.environ.get('MEETING_MINUTES_BROWSER'):
        path = Path(os.path.expandvars(str(explicit or os.environ['MEETING_MINUTES_BROWSER']))).expanduser()
        if not path.is_file():
            raise BrowserNotFound('指定的浏览器不存在：' + str(path))
        return path
    for value in CANDIDATES.get(sys.platform, []):
        path = Path(os.path.expandvars(value))
        if path.is_file():
            return path
    for command in COMMANDS:
        found = shutil.which(command)
        if found:
            return Path(found)
    bundled = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers')) / 'chromium'
    if bundled.is_file():
        return bundled
    raise BrowserNotFound('未找到 Chrome/Edge/Chromium；可设置 MEETING_MINUTES_BROWSER，或在 HTML 中点击“导出图片”。')


def inject(source):
    index = source.lower().rfind('</body>')
    if index < 0 or 'function makePng' not in source:
        raise ValueError('不是本技能生成的会议纪要 HTML。')
    return source[:index] + HOOK + source[index:]


def extract(dom):
    match = re.search(r'<pre id="headless-png" data-state="(ok|error)">([^<]*)</pre>', dom)
    if not match:
        raise RuntimeError('浏览器未完成图片导出。')
    if match.group(1) == 'error':
        raise ValueError('导出未完成：' + match.group(2))
    prefix = 'data:image/png;base64,'
    if not match.group(2).startswith(prefix):
        raise RuntimeError('浏览器返回的不是 PNG。')
    data = base64.b64decode(match.group(2)[len(prefix):], validate=True)
    if not data.startswith(PNG_SIGNATURE):
        raise RuntimeError('浏览器返回的不是 PNG。')
    return data


def size(data):
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


def render(html_path, out=None, browser=None, timeout=60):
    html_path = Path(html_path).expanduser().resolve()
    out = Path(out).expanduser().resolve() if out else html_path.with_suffix('.png')
    executable = find_browser(browser)
    with tempfile.TemporaryDirectory(prefix='meeting-png-') as tmp:
        page = Path(tmp) / 'page.html'
        page.write_text(inject(html_path.read_text(encoding='utf-8')), encoding='utf-8')
        command = [str(executable), '--headless=new', '--disable-gpu', '--hide-scrollbars',
                   '--no-first-run', '--no-default-browser-check', '--disable-extensions',
                   '--disable-background-networking', '--disable-component-update', '--disable-sync',
                   '--disable-default-apps', '--no-pings', '--metrics-recording-only',
                   '--user-data-dir=' + str(Path(tmp) / 'profile'),
                   '--virtual-time-budget=20000', '--dump-dom', page.as_uri()]
        if sys.platform.startswith('linux') and hasattr(os, 'geteuid') and os.geteuid() == 0:
            command.insert(1, '--no-sandbox')
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
        if result.returncode != 0 and 'headless-png' not in result.stdout:
            raise RuntimeError('浏览器渲染失败：' + (result.stderr.strip().splitlines() or ['未知错误'])[-1])
        data = extract(result.stdout)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name('.' + out.name + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(out)
    width, height = size(data)
    return {'png': str(out), 'width': width, 'height': height, 'browser': str(executable)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, required=True, help='生成的或用户保存过修改的纪要 HTML')
    parser.add_argument('--out', type=Path, help='PNG 路径；默认与 HTML 同名')
    parser.add_argument('--browser', help='Chrome/Edge/Chromium 可执行文件；也可用 MEETING_MINUTES_BROWSER')
    args = parser.parse_args()
    try:
        result = render(args.html, args.out, args.browser)
    except (BrowserNotFound, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
