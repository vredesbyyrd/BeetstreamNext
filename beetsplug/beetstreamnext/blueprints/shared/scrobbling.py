from __future__ import annotations

import hmac
import secrets
from typing import Callable
import flask

from beetsplug.beetstreamnext.blueprints import views
from beetsplug.beetstreamnext.core.runtime.logging import bsn_logger
from beetsplug.beetstreamnext.core.runtime.web import request_url
from beetsplug.beetstreamnext.core.services.scrobbling import SCROBBLERS, Scrobbler, ScrobbleError, link_account, unlink_account


def register_scrobbling_routes(bp: flask.Blueprint, login_required: Callable, home_url: Callable[[], str]) -> None:
    """
    Admin panel and account page expose the same routes, only the auth decorator (and where to land after linking) differ.
    """

    bp_name = bp.name

    def route(rule: str, endpoint: str, methods: list[str]) -> Callable:
        def register(view: Callable) -> Callable:
            bp.add_url_rule(rule, endpoint=endpoint, view_func=login_required(view), methods=methods)
            return view
        return register

    def scrobbler_or_404(service: str) -> Scrobbler:
        scrobbler = SCROBBLERS.get(service)
        if not scrobbler:
            flask.abort(404)
        return scrobbler

    @route('/scrobbling', 'route_scrobbling', ['GET'])
    def scrobbling() -> str:
        return views.render_scrobbling(flask.session['username'], bp_name=bp_name)

    @route('/scrobbling/<service>/connect', 'route_connect_scrobbler', ['GET'])
    def connect_scrobbler(service: str) -> flask.Response:
        scrobbler = scrobbler_or_404(service)
        if not scrobbler.available():
            flask.abort(404)

        # Ties the service's redirect back to this session
        state = secrets.token_urlsafe(16)
        flask.session['scrobbler_link_state'] = state

        callback_url = request_url(flask.url_for(f'{bp_name}.route_scrobbler_callback', service=service, state=state))
        return flask.redirect(scrobbler.auth_url(callback_url))

    @route('/scrobbling/<service>/callback', 'route_scrobbler_callback', ['GET'])
    def scrobbler_callback(service: str) -> flask.Response:
        scrobbler = scrobbler_or_404(service)
        expected_state = flask.session.pop('scrobbler_link_state', '')
        state = flask.request.args.get('state', '')
        token = flask.request.args.get('token', '')

        if not (expected_state and token and hmac.compare_digest(expected_state, state)):
            flask.flash(f'Could not link your {scrobbler.label} account, please try again.', 'error')
            return flask.redirect(home_url())

        try:
            session_key, account = scrobbler.link(token)
        except ScrobbleError as e:
            bsn_logger.warning(f"Linking {flask.session['username']}'s {scrobbler.label} account failed: {e}")
            flask.flash(f'{scrobbler.label} refused to link your account: {e}', 'error')
            return flask.redirect(home_url())

        link_account(flask.session['username'], service, session_key, account)
        flask.flash(f'Now scrobbling to {scrobbler.label} as {account}.', 'success')
        return flask.redirect(home_url())

    @route('/scrobbling/<service>/disconnect', 'route_disconnect_scrobbler', ['POST'])
    def disconnect_scrobbler(service: str) -> str:
        scrobbler = scrobbler_or_404(service)
        unlink_account(flask.session['username'], service)
        return views.render_scrobbling(flask.session['username'], f'Disconnected from {scrobbler.label}.', bp_name=bp_name)
