from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
import flask

from beetsplug.beetstreamnext.utils.db import get_beets_schema, library_cache
from beetsplug.beetstreamnext.utils.text import remove_accents, split_beets_multi


# Cleaned as Navidrome cleans text for search: quotes, brackets and commas go, slashes split words
_IGNORED_CHARS = re.compile(r"""[“”‘’'"\[\](){},]""")

# Chinese, Japanese and Korean words can't be expected to start a word: they match anywhere
_CJK = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]')

# A song's credits besides its artist and album artist: the contributors BSN lists, multi-valued
# fields first
_CREDIT_FIELDS = (
    ('artists',), ('composers', 'composer'), ('lyricists', 'lyricist'),
    ('arrangers', 'arranger'), ('remixers', 'remixer'),
)


def search_words(*texts: Optional[str]) -> List[str]:
    """Words of the texts as search compares them: lowercase, without accents or punctuation."""
    text = remove_accents(' '.join(t for t in texts if t)).lower()
    text = text.replace('/', ' ').replace('\\', ' ')
    return _IGNORED_CHARS.sub('', text).split()


def _searchable(*texts: Optional[str]) -> str:
    return f" {' '.join(search_words(*texts))} "


class _SearchIndex:
    """
    Searchable text of every song, album and album artist, read in one pass: normalising it is what
    costs, so it's done once per library change instead of for every search.

    A song is found by its title, album and credits, an album by its title, version, catalog number and
    every credit on it or its songs, an album artist by its name and sort name.
    """

    def __init__(self, lib):
        cols = get_beets_schema('items')
        credit_cols = ', '.join(next((f for f in fields if f in cols), 'NULL') for fields in _CREDIT_FIELDS)

        songs = []
        albums = []
        album_credits: Dict[int, Set[str]] = {}
        album_artists: Dict[str, str] = {}

        with lib.transaction() as tx:
            for song_id, album_id, title, album, artist, albumartist, *credited in tx.query(
                    f"""
                    SELECT id, album_id, title, album, artist, albumartist, {credit_cols}
                    FROM items
                    """):
                credits = [artist, albumartist, *(name for names in credited for name in split_beets_multi(names))]
                songs.append(((title or '').casefold(), song_id, _searchable(title, album, *credits)))
                if album_id:
                    album_credits.setdefault(album_id, set()).update(c for c in credits if c)

            for album_id, album, version, catalognum, albumartist, albumartists, sort_name in tx.query(
                    """
                    SELECT id, album, albumdisambig, catalognum, albumartist, albumartists, albumartist_sort
                    FROM albums
                    """):
                credits = [albumartist, *split_beets_multi(albumartists), *album_credits.get(album_id, ())]
                albums.append(((album or '').casefold(), album_id, _searchable(album, version, catalognum, *credits)))
                if albumartist:
                    album_artists.setdefault(albumartist, sort_name)

        # In the order search answers with: songs by title, albums and artists by name
        self._songs = sorted(songs)
        self._albums = sorted(albums)
        self._artists = sorted((name.casefold(), name, _searchable(name, sort_name))
                               for name, sort_name in album_artists.items())

    def find(self, words: List[str]) -> Tuple[List[int], List[int], List[str]]:
        """Song IDs, album IDs and album artist names whose text has every word, each starting a word."""
        needles = [word if _CJK.search(word) else f' {word}' for word in words]

        def matching(entries: List[Tuple[str, Any, str]]) -> List[Any]:
            return [key for _, key, text in entries if all(needle in text for needle in needles)]

        return matching(self._songs), matching(self._albums), matching(self._artists)


def search_index() -> _SearchIndex:
    cache = library_cache('search')
    if 'index' not in cache:
        cache['index'] = _SearchIndex(flask.g.lib)
    return cache['index']
