"""
/app/api/platform/* — the platform console's HTTP surface. Session cookie, like
the rest of /app/api, but every route here is guarded by
platform_admin.access.require_platform (users.platform_role), never by the
tenant role or tenant scope.

    GET  /app/api/platform/overview
    GET  /app/api/platform/tenants
    GET  /app/api/platform/tenants/{id}
    GET  /app/api/platform/audit?customer_id=&outcome=&tool=&before_id=&limit=
    POST /app/api/platform/tenants/{id}/suspend        {reason}      operator
    POST /app/api/platform/tenants/{id}/reactivate                   operator
    POST /app/api/platform/tenants/{id}/delete         {confirm_name} operator; suspended tenants only
    POST /app/api/platform/keys/{id}/revoke                          operator
    POST /app/api/platform/users/{id}/active           {active}      operator
    POST /app/api/platform/users/{id}/reset-password                 operator
    GET  /app/api/platform/alerts
    POST /app/api/platform/alerts/recipients           {protocol, endpoint}   operator
    POST /app/api/platform/alerts/recipients/remove    {id}                   operator
    GET  /app/api/platform/onboarding/options
    POST /app/api/platform/onboarding/tenants          {name, domain, vertical, admin_name, admin_email, data_origin?, tier?}  operator
    GET  /app/api/platform/onboarding/tenants/{id}
    POST /app/api/platform/onboarding/tenants/{id}/files     {file_type, csv_content, dry_run}  operator
    GET  /app/api/platform/onboarding/tenants/{id}/preflight
    POST /app/api/platform/onboarding/tenants/{id}/process   {mode?}                operator
    GET  /app/api/platform/onboarding/tenants/{id}/settings
    POST /app/api/platform/onboarding/tenants/{id}/settings/save  {values, recompute?}  operator

A refused attempt by a logged-in user is written to the audit log: somebody
without the operator flag asking for the cross-tenant view is worth knowing about.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from platform_admin import alerts, deletion, onboarding, service
from platform_admin.access import require_platform
from signal_engine.http import _json, _with_app

_ERRORS = {'not_authenticated': (401, 'not authenticated'),
           'not_operator': (403, 'the platform console is for platform operators only'),
           'read_only': (403, 'your platform access is read-only')}


async def _guard(request, name: str, write: bool = False):
    """(operator, error_response)."""
    def check():
        try:
            return require_platform(request, write=write), None
        except PermissionError as e:
            code = str(e)
            if code != 'not_authenticated':
                from app_api.auth import require_session
                from mcp_server import audit
                u = require_session(request)
                audit.record('ui', f'platform.{name}', None, key_kind='user', key_record=None, outcome='denied',
                             detail=f'user:{u.user_id} {code}')
            return None, code
    user, code = await _with_app(check)
    if code:
        status, message = _ERRORS.get(code, (403, 'forbidden'))
        return None, JSONResponse({'error': message}, status_code=status)
    return user, None


async def _act(fn):
    """Run a service action, mapping its exceptions to responses."""
    try:
        return JSONResponse(await _with_app(fn))
    except LookupError as e:
        return JSONResponse({'error': str(e)}, status_code=404)
    except PermissionError as e:
        return JSONResponse({'error': str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)


def register_platform_routes(mcp) -> None:

    @mcp.custom_route('/app/api/platform/overview', methods=['GET'], name='platform_overview')
    async def platform_overview(request):
        op, err = await _guard(request, 'overview')
        if err:
            return err
        return JSONResponse(await _with_app(service.overview))

    @mcp.custom_route('/app/api/platform/tenants', methods=['GET'], name='platform_tenants')
    async def platform_tenants(request):
        op, err = await _guard(request, 'tenants')
        if err:
            return err
        return JSONResponse({'tenants': await _with_app(service.list_tenants)})

    @mcp.custom_route('/app/api/platform/tenants/{customer_id:int}', methods=['GET'], name='platform_tenant')
    async def platform_tenant(request):
        op, err = await _guard(request, 'tenant_view')
        if err:
            return err
        detail = await _with_app(lambda: service.tenant_detail(op, request.path_params['customer_id']))
        if detail is None:
            return JSONResponse({'error': 'tenant not found'}, status_code=404)
        return JSONResponse(detail)

    @mcp.custom_route('/app/api/platform/audit', methods=['GET'], name='platform_audit')
    async def platform_audit(request):
        op, err = await _guard(request, 'audit')
        if err:
            return err
        p = request.query_params
        for name in ('customer_id', 'before_id', 'limit'):
            if p.get(name) and not p.get(name).isdigit():
                return JSONResponse({'error': f'{name} must be an integer'}, status_code=400)
        return JSONResponse(await _with_app(lambda: service.audit_log(
            customer_id=p.get('customer_id'), outcome=p.get('outcome'), tool=p.get('tool'),
            before_id=p.get('before_id'), limit=p.get('limit'))))

    @mcp.custom_route('/app/api/platform/tenants/{customer_id:int}/suspend', methods=['POST'], name='platform_tenant_suspend')
    async def platform_tenant_suspend(request):
        op, err = await _guard(request, 'tenant_suspend', write=True)
        if err:
            return err
        data = await _json(request)
        return await _act(lambda: service.suspend_tenant(op, request.path_params['customer_id'], data.get('reason')))

    @mcp.custom_route('/app/api/platform/tenants/{customer_id:int}/reactivate', methods=['POST'], name='platform_tenant_reactivate')
    async def platform_tenant_reactivate(request):
        op, err = await _guard(request, 'tenant_reactivate', write=True)
        if err:
            return err
        return await _act(lambda: service.reactivate_tenant(op, request.path_params['customer_id']))

    @mcp.custom_route('/app/api/platform/tenants/{customer_id:int}/delete', methods=['POST'], name='platform_tenant_delete')
    async def platform_tenant_delete(request):
        op, err = await _guard(request, 'tenant_delete', write=True)
        if err:
            return err
        data = await _json(request)
        return await _act(lambda: deletion.delete_tenant(op, request.path_params['customer_id'], data.get('confirm_name')))

    @mcp.custom_route('/app/api/platform/keys/{key_id:int}/revoke', methods=['POST'], name='platform_key_revoke')
    async def platform_key_revoke(request):
        op, err = await _guard(request, 'key_revoke', write=True)
        if err:
            return err
        return await _act(lambda: service.revoke_key(op, request.path_params['key_id']))

    @mcp.custom_route('/app/api/platform/users/{user_id:int}/active', methods=['POST'], name='platform_user_active')
    async def platform_user_active(request):
        op, err = await _guard(request, 'user_active', write=True)
        if err:
            return err
        data = await _json(request)
        if not isinstance(data.get('active'), bool):
            return JSONResponse({'error': 'active must be true or false'}, status_code=400)
        return await _act(lambda: service.set_user_active(op, request.path_params['user_id'], data['active']))

    @mcp.custom_route('/app/api/platform/users/{user_id:int}/reset-password', methods=['POST'], name='platform_user_reset_password')
    async def platform_user_reset_password(request):
        op, err = await _guard(request, 'user_reset_password', write=True)
        if err:
            return err
        return await _act(lambda: {
            'setup_token': service.reset_user_password(op, request.path_params['user_id']),
            'setup_token_note': 'Shown once — relay this to the user out of band.'})

    @mcp.custom_route('/app/api/platform/alerts', methods=['GET'], name='platform_alerts')
    async def platform_alerts(request):
        op, err = await _guard(request, 'alerts')
        if err:
            return err
        return JSONResponse(await _with_app(alerts.status))

    @mcp.custom_route('/app/api/platform/alerts/recipients', methods=['POST'], name='platform_alert_recipient_add')
    async def platform_alert_recipient_add(request):
        op, err = await _guard(request, 'alert_recipient_add', write=True)
        if err:
            return err
        data = await _json(request)
        return await _act(lambda: alerts.add_recipient(op, data.get('protocol'), data.get('endpoint')))

    @mcp.custom_route('/app/api/platform/alerts/recipients/remove', methods=['POST'], name='platform_alert_recipient_remove')
    async def platform_alert_recipient_remove(request):
        op, err = await _guard(request, 'alert_recipient_remove', write=True)
        if err:
            return err
        data = await _json(request)
        return await _act(lambda: alerts.remove_recipient(op, data.get('id')))


    # ── onboarding: create a tenant, stage its files, check them, load them ──

    @mcp.custom_route('/app/api/platform/onboarding/options', methods=['GET'], name='platform_onboarding_options')
    async def platform_onboarding_options(request):
        op, err = await _guard(request, 'onboard_options')
        if err:
            return err
        return await _act(lambda: onboarding.options(op))

    @mcp.custom_route('/app/api/platform/onboarding/tenants', methods=['POST'], name='platform_onboarding_create')
    async def platform_onboarding_create(request):
        op, err = await _guard(request, 'onboard_create', write=True)
        if err:
            return err
        data = await _json(request)
        return await _act(lambda: onboarding.create_tenant(op, data))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}', methods=['GET'], name='platform_onboarding_status')
    async def platform_onboarding_status(request):
        op, err = await _guard(request, 'onboard_status')
        if err:
            return err
        cid = request.path_params['customer_id']
        return await _act(lambda: onboarding.status(op, cid))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}/files', methods=['POST'], name='platform_onboarding_file')
    async def platform_onboarding_file(request):
        op, err = await _guard(request, 'onboard_upload', write=True)
        if err:
            return err
        cid = request.path_params['customer_id']
        data = await _json(request)
        return await _act(lambda: onboarding.stage_file(op, cid, data.get('file_type'), data.get('csv_content'),
                                                        bool(data.get('dry_run'))))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}/preflight', methods=['GET'], name='platform_onboarding_preflight')
    async def platform_onboarding_preflight(request):
        op, err = await _guard(request, 'onboard_preflight')
        if err:
            return err
        cid = request.path_params['customer_id']
        return await _act(lambda: onboarding.preflight_report(op, cid))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}/process', methods=['POST'], name='platform_onboarding_process')
    async def platform_onboarding_process(request):
        op, err = await _guard(request, 'onboard_process', write=True)
        if err:
            return err
        cid = request.path_params['customer_id']
        data = await _json(request)
        return await _act(lambda: onboarding.process(op, cid, data.get('mode')))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}/settings', methods=['GET'], name='platform_onboarding_settings')
    async def platform_onboarding_settings(request):
        op, err = await _guard(request, 'onboard_settings_read')
        if err:
            return err
        cid = request.path_params['customer_id']
        return await _act(lambda: onboarding.retention_settings(op, cid))

    @mcp.custom_route('/app/api/platform/onboarding/tenants/{customer_id:int}/settings/save', methods=['POST'], name='platform_onboarding_settings_save')
    async def platform_onboarding_settings_save(request):
        op, err = await _guard(request, 'onboard_settings_save', write=True)
        if err:
            return err
        cid = request.path_params['customer_id']
        data = await _json(request)
        return await _act(lambda: onboarding.save_retention_settings(op, cid, data))

ROUTES = ('/app/api/platform/overview', '/app/api/platform/tenants', '/app/api/platform/tenants/{id}',
          '/app/api/platform/audit', '/app/api/platform/tenants/{id}/suspend', '/app/api/platform/tenants/{id}/reactivate',
          '/app/api/platform/keys/{id}/revoke', '/app/api/platform/users/{id}/active',
          '/app/api/platform/users/{id}/reset-password', '/app/api/platform/alerts',
          '/app/api/platform/alerts/recipients', '/app/api/platform/alerts/recipients/remove',
          '/app/api/platform/tenants/{id}/delete',
          '/app/api/platform/onboarding/options', '/app/api/platform/onboarding/tenants',
          '/app/api/platform/onboarding/tenants/{id}', '/app/api/platform/onboarding/tenants/{id}/files',
          '/app/api/platform/onboarding/tenants/{id}/preflight', '/app/api/platform/onboarding/tenants/{id}/process',
          '/app/api/platform/onboarding/tenants/{id}/settings', '/app/api/platform/onboarding/tenants/{id}/settings/save')
