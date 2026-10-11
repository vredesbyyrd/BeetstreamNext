from __future__ import annotations

import json
import threading
import urllib.parse
from itertools import groupby
from typing import Any, Dict, List, Tuple

import pylast

from beetsplug.beetstreamnext.application import app, with_app_context
from beetsplug.beetstreamnext.core.runtime.logging import bsn_logger
from beetsplug.beetstreamnext.core.storage.connection import database
from beetsplug.beetstreamnext.core.storage.encryption import get_cipher


class ScrobbleError(Exception):
    """
    A scrobbling service refused or failed a call.
        retry: worth trying again later (service down, rate limited...)
        relink: the user's session is no longer valid, they have to link their account again
    """

    def __init__(self, message: str, retry: bool = False, relink: bool = False):
        super().__init__(message)
        self.retry = retry
        self.relink = relink


class Scrobbler:
    """
    An external scrobbling service. Users link their account once (the service redirects back with a
    token, which is exchanged for a session key), then their plays are submitted with that session.
    """

    key: str = ''           # stored with linked accounts and queued plays
    label: str = ''
    batch_size: int = 50    # most plays submitted in one call

    def available(self) -> bool:
        """Whether this server is configured for the service."""
        raise NotImplementedError

    def auth_url(self, callback_url: str) -> str:
        """Where to send the user to authorise BeetstreamNext. The service then redirects to `callback_url`."""
        raise NotImplementedError

    def link(self, token: str) -> Tuple[str, str]:
        """(session key, account name) for the token the service redirected back with."""
        raise NotImplementedError

    def now_playing(self, session_key: str, track: dict) -> None:
        raise NotImplementedError

    def scrobble(self, session_key: str, plays: List[dict]) -> None:
        """Submit plays: tracks with their 'timestamp' (unix time they were played), oldest first."""
        raise NotImplementedError


class LastFm(Scrobbler):
    """Last.fm, through pylast (a Libre.fm scrobbler would be this with pylast.LibreFMNetwork)."""

    key = 'lastfm'
    label = 'Last.fm'
    network_class = pylast.LastFMNetwork

    _RETRY_STATUSES = frozenset({
        pylast.STATUS_OPERATION_FAILED, pylast.STATUS_OFFLINE,
        pylast.STATUS_TEMPORARILY_UNAVAILABLE, pylast.STATUS_RATE_LIMIT_EXCEEDED,
    })
    _RELINK_STATUSES = frozenset({pylast.STATUS_INVALID_SK, pylast.STATUS_AUTH_FAILED})

    def _network(self, session_key: str = '') -> pylast._Network:
        return self.network_class(
            api_key=app.config['lastfm_api_key'],
            api_secret=app.config['lastfm_api_secret'],
            session_key=session_key,
        )

    def _call(self, fn, *args, **kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except pylast.WSError as e:
            status = e.get_id()
            raise ScrobbleError(str(e), retry=status in self._RETRY_STATUSES, relink=status in self._RELINK_STATUSES) from e
        except pylast.PyLastError as e:     # network errors, malformed responses
            raise ScrobbleError(str(e), retry=True) from e

    def available(self) -> bool:
        return bool(app.config.get('lastfm_api_key') and app.config.get('lastfm_api_secret'))

    def auth_url(self, callback_url: str) -> str:
        query = urllib.parse.urlencode({'api_key': app.config['lastfm_api_key'], 'cb': callback_url})
        return f'{self._network().homepage}/api/auth/?{query}'

    def link(self, token: str) -> Tuple[str, str]:
        generator = pylast.SessionKeyGenerator(self._network())
        return self._call(generator.get_web_auth_session_key_username, None, token)

    def now_playing(self, session_key: str, track: dict) -> None:
        self._call(self._network(session_key).update_now_playing, **track)

    def scrobble(self, session_key: str, plays: List[dict]) -> None:
        self._call(self._network(session_key).scrobble_many, plays)


SCROBBLERS: Dict[str, Scrobbler] = {s.key: s for s in (LastFm(),)}


def track_of(item) -> dict:
    """A beets item as scrobbling services take it (the keys pylast takes)."""
    track = {
        'artist': item.get('artist') or item.get('albumartist'),
        'title': item.get('title'),
        'album': item.get('album'),
        'album_artist': item.get('albumartist'),
        'track_number': item.get('track'),
        'duration': round(item.get('length') or 0),
        'mbid': item.get('mb_trackid'),
    }
    return {k: v for k, v in track.items() if v}


##
# Linked accounts

def linked_accounts(username: str) -> Dict[str, str]:
    """{service key: account name on that service} for the user's linked accounts."""

    with database() as db:
        rows = db.execute(
            """
            SELECT service, account
            FROM scrobbler_accounts
            WHERE username = ?
            """, (username,)
        ).fetchall()

    return {row['service']: row['account'] for row in rows}


def _session_keys(username: str) -> Dict[str, str]:

    with database() as db:
        rows = db.execute(
            """
            SELECT service, session_key
            FROM scrobbler_accounts
            WHERE username = ?
            """, (username,)
        ).fetchall()

    cipher = get_cipher()
    return {
        row['service']: (cipher.decrypt(row['session_key']) if cipher else row['session_key']).decode('utf-8')
        for row in rows
    }


def link_account(username: str, service: str, session_key: str, account: str) -> None:

    cipher = get_cipher()
    stored_key = cipher.encrypt(session_key.encode('utf-8')) if cipher else session_key.encode('utf-8')

    with database() as db:
        db.execute(
            """
            INSERT INTO scrobbler_accounts (username, service, session_key, account)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (username, service) DO UPDATE SET
                session_key = excluded.session_key, account = excluded.account, linked_at = unixepoch()
            """, (username, service, stored_key, account)
        )


def unlink_account(username: str, service: str) -> None:
    """Forget the user's session for this service, and the plays still waiting to be sent to it."""

    with database() as db:
        db.execute(
            """
            DELETE FROM scrobbler_accounts
            WHERE username = ? AND service = ?
            """, (username, service)
        )
        db.execute(
            """
            DELETE FROM scrobble_queue
            WHERE username = ? AND service = ?
            """, (username, service)
        )


##
# Now playing and scrobbles

def _send_now_playing(scrobbler: Scrobbler, session_key: str, track: dict) -> None:
    try:
        scrobbler.now_playing(session_key, track)
    except ScrobbleError as e:
        bsn_logger.debug(f'{scrobbler.label} now playing update failed: {e}')


def send_now_playing(username: str, track: dict) -> None:
    """
    Fire-and-forget: tell the user's linked services what they're playing.
    """
    for service, session_key in _session_keys(username).items():
        scrobbler = SCROBBLERS.get(service)
        if scrobbler and scrobbler.available():
            threading.Thread(target=_send_now_playing, args=(scrobbler, session_key, track),
                             name='now-playing', daemon=True).start()


def queue_scrobbles(username: str, plays: List[Tuple[dict, int]]) -> None:
    """
    Queue (track, played at) plays for each of the user's linked services, and submit them in the background.
    Queued plays survive the service (or this server) being down until they can be submitted.
    """
    services = [service for service in linked_accounts(username) if service in SCROBBLERS]
    if not services or not plays:
        return

    with database() as db:
        db.executemany(
            """
            INSERT INTO scrobble_queue (username, service, track, played_at)
            VALUES (?, ?, ?, ?)
            """, [(username, service, json.dumps(track), played_at) for service in services for track, played_at in plays]
        )

    submit_queued_scrobbles()


_submit_lock = threading.Lock()


def submit_queued_scrobbles() -> None:
    """Submit queued plays on a background thread."""
    threading.Thread(target=_submit_queue, name='scrobble', daemon=True).start()


def _submit_plays(username: str, service: str, rows: list) -> None:
    """Submit one user's queued plays to one service, deleting those it took (or refused for good)."""

    scrobbler = SCROBBLERS.get(service)
    session_key = _session_keys(username).get(service)
    if not scrobbler or not session_key or not scrobbler.available():
        return  # kept for when the service is configured again

    for start in range(0, len(rows), scrobbler.batch_size):
        batch = rows[start:start + scrobbler.batch_size]

        try:
            scrobbler.scrobble(session_key, [json.loads(row['track']) | {'timestamp': row['played_at']} for row in batch])

        except ScrobbleError as e:
            if e.relink:
                bsn_logger.warning(f"{scrobbler.label} rejected {username}'s session, unlinking their account: {e}")
                unlink_account(username, service)
                return
            if e.retry:
                bsn_logger.warning(f'Could not scrobble to {scrobbler.label} for {username}, will retry later: {e}')
                return
            bsn_logger.warning(f'{scrobbler.label} refused {len(batch)} scrobble(s) from {username}, dropping them: {e}')

        with database() as db:
            db.executemany(
                """
                DELETE FROM scrobble_queue
                WHERE id = ?
                """, [(row['id'],) for row in batch]
            )


@with_app_context
def _submit_queue() -> None:

    # One pass at a time, so no play is sent twice. A play queued meanwhile has its own pass waiting.
    with _submit_lock:
        try:
            with database() as db:
                rows = db.execute(
                    """
                    SELECT id, username, service, track, played_at
                    FROM scrobble_queue
                    ORDER BY username, service, played_at
                    """
                ).fetchall()

            for (username, service), group in groupby(rows, key=lambda row: (row['username'], row['service'])):
                _submit_plays(username, service, list(group))

        except Exception as e:
            bsn_logger.error(f'Scrobble submission failed: {e}')
