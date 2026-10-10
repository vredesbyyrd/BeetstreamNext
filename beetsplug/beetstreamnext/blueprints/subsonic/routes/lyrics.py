from __future__ import annotations
import re
from typing import TYPE_CHECKING, Tuple, Union
import flask

from .. import subsonic_bp

from beetsplug.beetstreamnext.utils.text import safe_str
from beetsplug.beetstreamnext.utils.general import api_bool
from beetsplug.beetstreamnext.blueprints.subsonic.responses import subsonic_response, subsonic_error
from beetsplug.beetstreamnext.core.runtime.logging import bsn_logger
from beetsplug.beetstreamnext.core.library.resolve import Resolve

from beetsplug.beetstreamnext.application import app

if TYPE_CHECKING:
    from beetsplug.lyrics import LyricsPlugin
    from beets.plugins import BeetsPlugin


# to detect LRC timestamps like [00:12.34] (several per line), and ID tags like [ar:...] or [offset:+250]
_LRC_TIMESTAMP_REGEX = re.compile(r'\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]')
_LRC_TAG_REGEX = re.compile(r'^\[([a-z]+):(.*)\]$')


def _get_lyrics_plugin() -> Union['BeetsPlugin', 'LyricsPlugin', None]:
    """
    Find the beets lyrics plugin instance. Should work with any version of Beets.
    """
    from beets import plugins
    for p in plugins.find_plugins():
        if p.name == 'lyrics':
            return p
    try:
        from beetsplug.lyrics import LyricsPlugin
        return LyricsPlugin()
    except (ImportError, Exception) as e:
        bsn_logger.error(f'Could not load beets lyrics plugin: {e}')
        return None


def _parse_lyrics_content(text: str) -> Tuple[list, bool, int]:
    """
    Parses raw lyrics text.
    Detects if it's synced (LRC) and returns structured lines, and the LRC offset (ms).
    """
    timed_lines = []
    plain_lines = []
    offset = 0

    for line in text.splitlines():
        line = line.strip()

        match = _LRC_TAG_REGEX.match(line)
        if match:
            if match.group(1) == 'offset':
                try:
                    offset = int(match.group(2))
                except ValueError:
                    pass
            continue

        starts = []
        while match := _LRC_TIMESTAMP_REGEX.match(line):
            minutes, seconds, fraction = match.groups()
            starts.append((int(minutes) * 60 + int(seconds)) * 1000 + int((fraction or '0').ljust(3, '0')))
            line = line[match.end():]

        if starts:
            timed_lines.extend({'start': start, 'value': line.strip()} for start in starts)
        elif line:
            plain_lines.append({'value': line})

    if timed_lines:
        return sorted(timed_lines, key=lambda line: line['start']), True, offset

    return plain_lines, False, 0


def _fetch_lyrics_data(item) -> dict | None:
    if not item:
        return None

    # Check database first
    lyrics_text = item.get('lyrics')

    if not lyrics_text:
        if not app.config.get('fetch_lyrics'):
            bsn_logger.info('No lyrics in database, and `fetch_lyrics` not enabled in config. Skipping.')
        else:
            lyrics_plugin = _get_lyrics_plugin()
            if lyrics_plugin:
                try:
                    if app.config.get('save_lyrics'):
                        lyrics_plugin.add_item_lyrics(item, write=False)  # write is 'write to song file' so NOPE
                        lyrics_text = item.get('lyrics')
                    else:
                        lyrics = lyrics_plugin.find_lyrics(item)
                        lyrics_text = lyrics.text
                except Exception as e:
                    bsn_logger.error(f'Error calling lyrics plugin: {e}')
            else:
                bsn_logger.info('Lyrics plugin not found in beets. Is it enabled?')

    if not lyrics_text:
        return None

    return {
        'text': str(lyrics_text),
        'lang': item.get('lyrics_language') or 'xxx',   # OpenSubsonic's fallback for unknown language is xxx
        'instrumental': bool(item.get('lyrics_instrumental')),
        'artist': item.get('artist') or '',
        'title': item.get('title') or ''
    }


# Spec: https://opensubsonic.netlify.app/docs/endpoints/getLyrics/
@subsonic_bp.route('/getLyrics', methods=['GET', 'POST'])
@subsonic_bp.route('/getLyrics.view', methods=['GET', 'POST'])
def endpoint_get_lyrics() -> flask.Response:
    r = flask.request.values
    resp_fmt = r.get('f', default='xml', type=safe_str)
    artist = r.get('artist', default='', type=safe_str)
    title = r.get('title', default='', type=safe_str)

    if not artist or not title:
        return subsonic_error(10, resp_fmt=resp_fmt)

    with flask.g.lib.transaction() as tx:
        rows = tx.query(
            """
            SELECT id FROM items 
            WHERE lower(artist) = lower(?) AND lower(title) = lower(?) 
            LIMIT 1
            """, (artist, title)
        )

    if not rows:
        return subsonic_error(70, message='Song not found.', resp_fmt=resp_fmt)

    item = flask.g.lib.get_item(rows[0][0])
    data = _fetch_lyrics_data(item)

    if not data:
        return subsonic_error(70, message='Lyrics not found.', resp_fmt=resp_fmt)

    payload = {
        'lyrics': {
            'artist': data['artist'],
            'title': data['title'],
            'value': data['text']
        }
    }
    return subsonic_response(payload, resp_fmt)


# Spec: https://opensubsonic.netlify.app/docs/endpoints/getLyricsBySongId/
@subsonic_bp.route('/getLyricsBySongId', methods=['GET', 'POST'])
@subsonic_bp.route('/getLyricsBySongId.view', methods=['GET', 'POST'])
def endpoint_get_lyrics_by_song_id() -> flask.Response:
    r = flask.request.values
    resp_fmt = r.get('f', default='xml', type=safe_str)
    req_id = r.get('id', default='', type=safe_str)      # Required
    enhanced = r.get('enhanced', default=False, type=api_bool)

    if not req_id:
        return subsonic_error(10, resp_fmt=resp_fmt)

    item = Resolve.song(req_id)

    if not item:
        return subsonic_error(70, message='Song not found.', resp_fmt=resp_fmt)

    data = _fetch_lyrics_data(item)
    if not data:
        return subsonic_response({'lyricsList': {'structuredLyrics': []}}, resp_fmt=resp_fmt)

    lines, has_timestamps, offset = _parse_lyrics_content(data['text'])

    structured = {
        'displayArtist': data['artist'],
        'displayTitle': data['title'],
        'lang': data['lang'],
        'synced': has_timestamps,
        'line': lines
    }
    if offset:
        structured['offset'] = offset
    if enhanced:
        structured['kind'] = 'main'

    payload = {
        'lyricsList': {
            'structuredLyrics': [structured]
        }
    }
    return subsonic_response(payload, resp_fmt=resp_fmt)