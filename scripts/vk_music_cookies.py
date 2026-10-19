"""Take the VK session from a local browser and save it as VK_MUSIC_COOKIES.

Bot and worker need only two cookies (remixsid on vk.ru and p on login.vk.ru)
to get VK web tokens for music. Close the browser first (cookie DB is locked
while it runs), then:

    python scripts/vk_music_cookies.py              # Yandex Browser, Default profile
    python scripts/vk_music_cookies.py "<profile dir>" [chrome|edge|...]

Requires yt-dlp installed locally (pip install yt-dlp). The line is written to
envs/secrets.env; copy it to the server's envs/secrets.env as well.
"""

import os
import sys
from pathlib import Path

from yt_dlp.cookies import extract_cookies_from_browser

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'yt_shared' / 'src'))

from yt_shared.vk_music import VkApiError, vk_api  # noqa: E402

SECRETS = ROOT / 'envs' / 'secrets.env'
DEFAULT_PROFILE = os.path.expandvars(
    r'%LOCALAPPDATA%\Yandex\YandexBrowser\User Data\Default'
)
NEEDED = {('vk.ru', 'remixsid'), ('login.vk.ru', 'p')}


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROFILE
    browser = sys.argv[2] if len(sys.argv) > 2 else 'chrome'
    jar = extract_cookies_from_browser(browser, profile)
    found = {
        (c.domain.lstrip('.'), c.name): c.value
        for c in jar
        if (c.domain.lstrip('.'), c.name) in NEEDED
    }
    missing = NEEDED - found.keys()
    if missing:
        sys.exit(f'Не нашёл куки {sorted(missing)} — залогинься в ВК (vk.ru) в этом браузере.')
    cookies = '; '.join(f'{name}={value}' for (_, name), value in sorted(found.items()))

    os.environ['VK_MUSIC_COOKIES'] = cookies
    try:
        items = vk_api('audio.search', {'q': 'noize mc', 'count': 3})['items']
    except VkApiError as err:
        sys.exit(f'Куки не подошли: {err}')
    print('Поиск работает:', '; '.join(f'{i["artist"]} - {i["title"]}' for i in items))

    lines = SECRETS.read_text(encoding='utf-8').splitlines() if SECRETS.exists() else []
    lines = [ln for ln in lines if not ln.startswith('VK_MUSIC_COOKIES=')]
    lines.append(f'VK_MUSIC_COOKIES={cookies}')
    SECRETS.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'Записал VK_MUSIC_COOKIES в {SECRETS}')


if __name__ == '__main__':
    main()
