"""
The human UI's HTTP surface (Starlette, mounted beside /mcp): session cookies,
not Bearer keys (app_api/auth.py). Every handler calls straight into the same
service functions the Bearer-keyed /api/* routes and MCP tools use — see
docs/design/ui-rbac.md §4 for the route → function table.

    POST /app/api/auth/login | /logout | /set-password         no session required (login), or a valid setup token
    GET  /app/api/me
    GET  /app/api/portfolio?customer_id=
    GET  /app/api/accounts/{account_id}?customer_id=
    GET  /app/api/interventions | POST .../evaluate | .../{id}/approve | .../{id}/report
    GET  /app/api/roi | /app/api/roi/priorities | /app/api/roi/power-of-1
    GET  /app/api/calibrations | POST .../propose | .../{id}/approve | .../{id}/reject      (admin)
    GET  /app/api/review-queue | POST /app/api/review                                        (csm/admin)
    GET  /app/api/playbooks/config | POST /app/api/playbooks/config                          (admin)
    GET  /app/api/users | POST /app/api/users | PATCH /app/api/users/{id} | POST .../{id}/reset-password  (admin)
    GET  /app/api/ask/questions | POST /app/api/ask                                              (every role)
"""
from __future__ import annotations

import os

from starlette.responses import JSONResponse

from app_api import settings
from app_api.auth import AuthError, allows_account, allows_customer, require_session, user_scope
from signal_engine.http import _json, _with_app

_INSECURE_ENV = 'SESSION_COOKIE_INSECURE'   # tests / a local http-only dev box; never set on the box


def _cookie_kwargs() -> dict:
    return {'httponly': True, 'samesite': 'lax', 'secure': os.environ.get(_INSECURE_ENV, '').lower() not in ('true', '1', 'yes')}


def _guard(request, role: str = None):
    """(user, error_response). error_response is set on 401/403; the caller returns it as-is."""
    try:
        user = _with_app(lambda: require_session(request, role))
        return user, None
    except PermissionError as e:
        status = 403 if str(e) == 'wrong_role' else 401
        return None, JSONResponse({'error': 'not authenticated' if status == 401 else 'forbidden for this role'}, status_code=status)


def _forbidden_scope():
    return JSONResponse({'error': 'not permitted for your account/tenant scope'}, status_code=403)


def _me_view(u) -> dict:
    """The session-user shape for /app/api/me and the login response — a SessionUser
    snapshot, never the ORM row (see app_api.auth.SessionUser). Deliberately smaller than
    app_api.users._view (no password_hash presence, no last_login) — /me is not the admin
    user-management listing."""
    return {'user_id': u.user_id, 'customer_id': u.customer_id, 'email': u.email, 'name': u.name,
            'role': u.role, 'allowed_customer_ids': u.allowed_customer_ids, 'allowed_account_ids': u.allowed_account_ids}


def register_app_api_routes(mcp) -> None:
    from journeys.read import list_journeys, get_journey, origin_block
    from playbooks import governance as gov
    from playbooks.definitions import playbooks_for_customer, configure_tenant
    from roi.priorities import investment_priorities
    from roi.power_of_1 import power_of_1
    from roi.measured import roi as roi_measured
    from wizards import wizard_c_calibration as wc
    from wizards.wizard_d_foresight import get_forecast
    from signal_engine.ingest_api import review_queue
    from signal_engine.review import review_signal
    from app_api import users as user_admin

    # ── auth ──

    @mcp.custom_route('/app/api/auth/login', methods=['POST'], name='ui_login')
    async def ui_login(request):
        data = await _json(request)
        from app_api.auth import login_session
        ip = request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else 'unknown')
        try:
            token, user = _with_app(lambda: login_session(data.get('email'), data.get('password'), ip))
        except AuthError as e:
            return JSONResponse({'error': str(e)}, status_code=401)
        resp = JSONResponse({'user': _me_view(user)})
        resp.set_cookie(settings.get('session', 'cookie_name'), token, max_age=int(settings.get('session', 'max_age_seconds')), **_cookie_kwargs())
        return resp

    @mcp.custom_route('/app/api/auth/logout', methods=['POST'], name='ui_logout')
    async def ui_logout(request):
        resp = JSONResponse({'status': 'ok'})
        resp.delete_cookie(settings.get('session', 'cookie_name'))
        return resp

    @mcp.custom_route('/app/api/auth/set-password', methods=['POST'], name='ui_set_password')
    async def ui_set_password(request):
        data = await _json(request)
        from app_api.auth import consume_setup_token
        try:
            _with_app(lambda: consume_setup_token(data.get('token'), data.get('new_password')))
        except AuthError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        return JSONResponse({'status': 'ok'})

    @mcp.custom_route('/app/api/me', methods=['GET'], name='ui_me')
    async def ui_me(request):
        user, err = _guard(request)
        if err:
            return err
        return JSONResponse(_me_view(user))     # user is already a resolved SessionUser snapshot — no DB access needed

    # ── portfolio / accounts ──

    @mcp.custom_route('/app/api/portfolio', methods=['GET'], name='ui_portfolio')
    async def ui_portfolio(request):
        user, err = _guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        if not allows_customer(user, int(cid)):
            return _forbidden_scope()
        rows = _with_app(lambda: list_journeys(int(cid)))
        _, aids = user_scope(user)
        if aids is not None:
            rows = [r for r in rows if r['account_id'] in aids]
        return JSONResponse({'accounts': rows, **_with_app(lambda: origin_block(int(cid)))})

    @mcp.custom_route('/app/api/accounts/{account_id:int}', methods=['GET'], name='ui_account')
    async def ui_account(request):
        user, err = _guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        aid = request.path_params['account_id']
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        if not allows_customer(user, int(cid)) or not allows_account(user, aid):
            return _forbidden_scope()
        j = _with_app(lambda: get_journey(int(cid), aid, compact=False))
        if j is None:
            return JSONResponse({'error': 'no journey for this account'}, status_code=404)
        return JSONResponse(j)

    # ── interventions ──

    @mcp.custom_route('/app/api/interventions', methods=['GET'], name='ui_interventions_list')
    async def ui_interventions_list(request):
        user, err = _guard(request)
        if err:
            return err
        q = request.query_params
        cid = q.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        aid = q.get('account_id')
        if aid and not allows_account(user, int(aid)):
            return _forbidden_scope()
        out = _with_app(lambda: gov.list_interventions(int(cid), aid, q.get('state')))
        _, aids = user_scope(user)
        if aids is not None:
            out['interventions'] = [v for v in out['interventions'] if v['account_id'] in aids]
        return JSONResponse(out)

    @mcp.custom_route('/app/api/interventions/{intervention_id:int}/approve', methods=['POST'], name='ui_interventions_approve')
    async def ui_interventions_approve(request):
        user, err = _guard(request, role='csm')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            actor = {'key_kind': 'user', 'key_record': None, 'key_id': None, 'label': f'user:{user.user_id}'}
            return JSONResponse(_with_app(lambda: gov.approve(int(cid), request.path_params['intervention_id'], data.get('note'), actor=actor)))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/app/api/interventions/{intervention_id:int}/report', methods=['POST'], name='ui_interventions_report')
    async def ui_interventions_report(request):
        user, err = _guard(request, role='csm')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            actor = {'key_kind': 'user', 'key_record': None, 'key_id': None, 'label': f'user:{user.user_id}'}
            return JSONResponse(_with_app(lambda: gov.report(int(cid), request.path_params['intervention_id'], data.get('state'),
                                                             note=data.get('note'), outcome_type=data.get('outcome_type'),
                                                             outcome_date=data.get('outcome_date'), revenue=data.get('revenue'), actor=actor)))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    # ── ROI / Power-of-1 (cfo, cro, admin) ──

    def _finance_guard(request):
        user, err = _guard(request)
        if err:
            return None, err
        if user.role not in ('cfo', 'cro', 'admin'):
            return None, JSONResponse({'error': 'forbidden for this role'}, status_code=403)
        return user, None

    @mcp.custom_route('/app/api/roi/priorities', methods=['GET'], name='ui_roi_priorities')
    async def ui_roi_priorities(request):
        user, err = _finance_guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: investment_priorities(int(cid))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=422)

    @mcp.custom_route('/app/api/roi/power-of-1', methods=['GET'], name='ui_roi_po1')
    async def ui_roi_po1(request):
        user, err = _finance_guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: power_of_1(int(cid))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=422)

    @mcp.custom_route('/app/api/roi', methods=['GET'], name='ui_roi_measured')
    async def ui_roi_measured(request):
        user, err = _finance_guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: roi_measured(int(cid))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=422)

    @mcp.custom_route('/app/api/forecast', methods=['GET'], name='ui_forecast')
    async def ui_forecast(request):
        user, err = _guard(request)
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        aid = request.query_params.get('account_id')
        if aid and not allows_account(user, int(aid)):
            return _forbidden_scope()
        res = _with_app(lambda: get_forecast(int(cid), int(aid) if aid else None))
        if res is None:
            return JSONResponse({'error': 'no Foresight run yet'}, status_code=404)
        return JSONResponse(res)

    # ── ask ai (every role; admin may also preview any role's curated set) ──

    @mcp.custom_route('/app/api/ask/questions', methods=['GET'], name='ui_ask_questions')
    async def ui_ask_questions(request):
        user, err = _guard(request)
        if err:
            return err
        from ask_ai import settings as ask_ai_settings
        by_role = ask_ai_settings.curated_questions()
        # non-admin roles only ever see their own set; admin gets all sets, to preview/use any of them
        visible = by_role if user.role == 'admin' else {user.role: by_role.get(user.role, [])}
        return JSONResponse({'roles': list(by_role.keys()), 'questions': visible})

    @mcp.custom_route('/app/api/ask', methods=['POST'], name='ui_ask')
    async def ui_ask(request):
        user, err = _guard(request)
        if err:
            return err
        try:
            data = await request.json()
        except Exception:
            data = {}
        cid = data.get('customer_id')
        if not cid or not (data.get('question') or '').strip():
            return JSONResponse({'error': 'customer_id and question are required'}, status_code=400)
        if not allows_customer(user, int(cid)):
            return _forbidden_scope()
        aid = data.get('account_id')
        if aid and not allows_account(user, int(aid)):
            return _forbidden_scope()
        from ask_ai.answer import ask as ask_ai_ask
        try:
            res = _with_app(lambda: ask_ai_ask(int(cid), data['question'], account_id=int(aid) if aid else None, as_of=data.get('as_of')))
        except LookupError as e:
            return JSONResponse({'error': str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({'error': f'answer failed: {str(e)[:200]}'}, status_code=502)
        return JSONResponse(res)

    # ── calibrations (admin) ──

    @mcp.custom_route('/app/api/calibrations', methods=['GET'], name='ui_calibrations_get')
    async def ui_calibrations_get(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        q = request.query_params
        cid = q.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        pid = q.get('proposal_id')
        return JSONResponse(_with_app(lambda: wc.get_calibration(int(cid), int(pid) if pid else None)))

    @mcp.custom_route('/app/api/calibrations/propose', methods=['POST'], name='ui_calibrations_propose')
    async def ui_calibrations_propose(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: wc.propose(int(cid))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/app/api/calibrations/{proposal_id:int}/approve', methods=['POST'], name='ui_calibrations_approve')
    async def ui_calibrations_approve(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: wc.approve(int(cid), request.path_params['proposal_id'], note=data.get('note'))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/app/api/calibrations/{proposal_id:int}/reject', methods=['POST'], name='ui_calibrations_reject')
    async def ui_calibrations_reject(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: wc.reject(int(cid), request.path_params['proposal_id'], note=data.get('note'))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    # ── review queue (csm, admin) ──

    @mcp.custom_route('/app/api/review-queue', methods=['GET'], name='ui_review_queue')
    async def ui_review_queue_route(request):
        user, err = _guard(request, role='csm')
        if err:
            return err
        q = request.query_params
        cid = q.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        status, body = _with_app(lambda: review_queue(int(cid), q.get('account_id'), q.get('urgency'),
                                                       int(q.get('page', 1)), int(q.get('per_page', 25))))
        return JSONResponse(body, status_code=status)

    @mcp.custom_route('/app/api/review', methods=['POST'], name='ui_review_post')
    async def ui_review_post(request):
        user, err = _guard(request, role='csm')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid or not allows_customer(user, int(cid)):
            return _forbidden_scope() if cid else JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: review_signal(
                int(cid), data.get('signal_id'), data.get('decision'), subtype=data.get('subtype'),
                node_id=data.get('node_id'), note=data.get('note'), reviewer=f'user:{user.user_id}')))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    # ── playbook config (admin) ──

    @mcp.custom_route('/app/api/playbooks/config', methods=['GET'], name='ui_playbooks_config_get')
    async def ui_playbooks_config_get(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        cid = request.query_params.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        return JSONResponse(_with_app(lambda: playbooks_for_customer(int(cid))))

    @mcp.custom_route('/app/api/playbooks/config', methods=['POST'], name='ui_playbooks_config_post')
    async def ui_playbooks_config_post(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        cid = data.get('customer_id')
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: configure_tenant(
                int(cid), webhook_url=data.get('webhook_url'), webhook_secret=data.get('webhook_secret'),
                disabled_playbooks=data.get('disabled_playbooks'), automation_level=data.get('automation_level'),
                kill_switch=data.get('kill_switch'))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    # ── users (admin) ──

    @mcp.custom_route('/app/api/users', methods=['GET'], name='ui_users_list')
    async def ui_users_list(request):
        user, err = _guard(request, role='admin')
        if err:
            return err
        cid = request.query_params.get('customer_id')
        return JSONResponse({'users': _with_app(lambda: user_admin.list_users(int(cid) if cid else None))})

    @mcp.custom_route('/app/api/users', methods=['POST'], name='ui_users_invite')
    async def ui_users_invite(request):
        admin, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        try:
            user_dict, raw = _with_app(lambda: user_admin.invite(admin, data.get('customer_id'), data.get('email'), data.get('name'),
                                                                 data.get('role'), data.get('allowed_account_ids')))
            return JSONResponse({'user': user_dict, 'setup_token': raw,
                                 'setup_token_note': 'Shown once — relay this to the new user out of band.'})
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/app/api/users/{user_id:int}', methods=['PATCH'], name='ui_users_patch')
    async def ui_users_patch(request):
        admin, err = _guard(request, role='admin')
        if err:
            return err
        data = await _json(request)
        try:
            return JSONResponse(_with_app(lambda: user_admin.patch_user(
                admin, request.path_params['user_id'], role=data.get('role'), active=data.get('active'),
                allowed_customer_ids=data.get('allowed_customer_ids'), allowed_account_ids=data.get('allowed_account_ids'))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/app/api/users/{user_id:int}/reset-password', methods=['POST'], name='ui_users_reset_password')
    async def ui_users_reset_password(request):
        admin, err = _guard(request, role='admin')
        if err:
            return err
        try:
            raw = _with_app(lambda: user_admin.reset_password(admin, request.path_params['user_id']))
            return JSONResponse({'setup_token': raw, 'setup_token_note': 'Shown once — relay this to the user out of band.'})
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)


ROUTES = ('/app/api/auth/login', '/app/api/auth/logout', '/app/api/auth/set-password', '/app/api/me',
          '/app/api/portfolio', '/app/api/accounts/{id}', '/app/api/interventions', '/app/api/interventions/{id}/approve',
          '/app/api/interventions/{id}/report', '/app/api/roi', '/app/api/roi/priorities', '/app/api/roi/power-of-1',
          '/app/api/forecast', '/app/api/calibrations', '/app/api/calibrations/propose', '/app/api/calibrations/{id}/approve',
          '/app/api/calibrations/{id}/reject', '/app/api/review-queue', '/app/api/review', '/app/api/playbooks/config',
          '/app/api/users', '/app/api/users/{id}', '/app/api/users/{id}/reset-password',
          '/app/api/ask/questions', '/app/api/ask')
